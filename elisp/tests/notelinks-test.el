;;; notelinks-test.el --- ERT tests for notelinks.el -*- lexical-binding: t; -*-

;;; Commentary:

;; Run with:
;;   emacs -Q --batch -L elisp -L elisp/tests -l elisp/tests/notelinks-test.el \
;;         -f ert-run-tests-batch-and-exit
;;
;; The engine is stubbed: instead of spawning the CLI we feed
;; `notelinks--on-result' a parsed JSON envelope.  Fixtures live next to this
;; file (`sample_output.json' + `epistemic_uncertainty.org').

;;; Code:

(require 'cl-lib)
(require 'ert)

(defvar notelinks-test--dir
  (file-name-directory (or load-file-name buffer-file-name default-directory))
  "Directory containing this test file and its fixtures.")

;; Make notelinks.el (one level up) loadable regardless of the caller's -L flags.
(add-to-list 'load-path (expand-file-name ".." notelinks-test--dir))
(require 'notelinks)

(defun notelinks-test--file (name)
  (expand-file-name name notelinks-test--dir))

(defun notelinks-test--read (name)
  (with-temp-buffer (insert-file-contents (notelinks-test--file name)) (buffer-string)))

(defun notelinks-test--accept-all ()
  "Accept every live suggestion (point-driven, like the user would)."
  (dolist (s (copy-sequence notelinks--suggestions))
    (goto-char (overlay-start (notelinks-sug-overlay s)))
    (notelinks-accept)))

(defmacro notelinks-test--with-review (content env &rest body)
  "Run BODY in a temp Org buffer holding CONTENT after reviewing ENV.
ENV is an already-built envelope alist.  `content' is bound for BODY."
  (declare (indent 2))
  `(let ((content ,content))
     (with-temp-buffer
       (insert content)
       (org-mode)
       (cl-letf (((symbol-function 'notelinks--show-panel) #'ignore)
                 ((symbol-function 'notelinks--report) (lambda (&rest _) nil)))
         (notelinks--on-result (current-buffer) ,env))
       ,@body)))

(defun notelinks-test--fixture-env ()
  (json-parse-string (notelinks-test--read "sample_output.json")
                     :object-type 'alist :array-type 'list
                     :null-object nil :false-object nil))

;;;; Unit tests — link assembly

(ert-deftest notelinks-test-assemble-file-level ()
  (should (equal "[[id:FID][My Note]]"
                 (notelinks--assemble-link '((file_id . "FID") (title . "My Note")
                                             (heading))
                                           "My Note"))))

(ert-deftest notelinks-test-assemble-heading-with-id ()
  (should (equal "[[id:HID][desc]]"
                 (notelinks--assemble-link
                  '((file_id . "FID") (title . "T")
                    (heading . ((text . "Some Heading") (id . "HID") (level . 2))))
                  "desc"))))

(ert-deftest notelinks-test-assemble-heading-no-id-fallback ()
  (should (equal "[[id:FID::*Some Heading][desc]]"
                 (notelinks--assemble-link
                  '((file_id . "FID") (title . "T")
                    (heading . ((text . "Some Heading") (id) (level . 1))))
                  "desc"))))

(ert-deftest notelinks-test-insert-text-uses-heading-text-as-desc ()
  ;; Insert to a heading target: description is the heading text, not the
  ;; engine's link_description.
  (let ((s (notelinks--make-sug
            '((id . "h") (type . "elaborates") (confidence . 3) (why . "w")
              (target_excerpt . "e")
              (target . ((file . "f.org") (title . "T") (file_id . "FID")
                         (heading . ((text . "Heading Text") (id . "HID") (level . 1)))))
              (source_anchor . ((char_start . 0) (char_end . 0)
                                (expect . "") (before . "x ") (after . "y")
                                (template . "{{link}}")
                                (link_description . "ENGINE-DESC")))))))
    (should (string= "[[id:HID][Heading Text]]" (notelinks--insert-text s))))
  ;; File-level target (no heading) keeps the engine's link_description.
  (let ((s (notelinks--make-sug
            (notelinks-test--sug "f" 3 "" "x " "y" "{{link}}" "ENGINE-DESC"))))
    (should (string= "[[id:FID][ENGINE-DESC]]" (notelinks--insert-text s)))))

;;;; Unit tests — template filling

(ert-deftest notelinks-test-fill-substitutes-link ()
  (should (equal "see [[id:x][y]] now"
                 (notelinks--fill "see {{link}} now" "[[id:x][y]]"))))

(ert-deftest notelinks-test-fill-default-template ()
  (should (equal "[[id:x][y]]" (notelinks--fill nil "[[id:x][y]]"))))

;;;; Fixture-driven — resolution, accept, reject

(ert-deftest notelinks-test-resolves-all-fixture-suggestions ()
  ;; Keep every fixture suggestion (it spans confidence 1–3) to exercise
  ;; resolution; the confidence gate is covered separately.
  (let ((notelinks-min-confidence 1))
    (notelinks-test--with-review (notelinks-test--read "epistemic_uncertainty.org")
        (notelinks-test--fixture-env)
      (should (= 4 (length notelinks--suggestions))))))

(ert-deftest notelinks-test-accept-all-produces-correct-links ()
  (let ((notelinks-min-confidence 1))
   (notelinks-test--with-review (notelinks-test--read "epistemic_uncertainty.org")
      (notelinks-test--fixture-env)
    (notelinks-test--accept-all)
    (should (= 0 (length notelinks--suggestions)))
    (let ((txt (buffer-string)))
      ;; insert anchor -> heading-with-id link inside engine-authored prose;
      ;; a heading target uses the heading text ("Differential Entropy") as desc
      ;; (not the engine's link_description "Mutual Information").
      (should (string-match-p
               (regexp-quote "[[id:6398DC98-3FD0-45B5-B2CA-E0D8E81F5583][Differential Entropy]]") txt))
      ;; wrap-span -> heading-with-id
      (should (string-match-p
               (regexp-quote "[[id:9E185F19-501A-4C4C-BD7B-8D57105C70AE][gaussain]]") txt))
      ;; wrap-span -> file-level (mention)
      (should (string-match-p
               (regexp-quote "[[id:40628C21-A838-45DA-836C-2FA6E9F3B4E6][entropy of the mixture]]") txt))
      ;; wrap-span -> no-id heading fallback (::*Heading)
      (should (string-match-p
               (regexp-quote "[[id:40628C21-A838-45DA-836C-2FA6E9F3B4E6::*Statistical Mechanics][averaged over all models]]")
               txt))))))

(ert-deftest notelinks-test-reject-all-restores-buffer ()
  (notelinks-test--with-review (notelinks-test--read "epistemic_uncertainty.org")
      (notelinks-test--fixture-env)
    ;; insert anchor pre-inserted its prose before review
    (should (string-match-p "decomposition is the" (buffer-string)))
    (notelinks-quit)
    (should (string= content (buffer-string)))))

(ert-deftest notelinks-test-accept-then-reject-rest-is-byte-clean ()
  "Accepting one then rejecting the rest leaves exactly one link added."
  (notelinks-test--with-review (notelinks-test--read "epistemic_uncertainty.org")
      (notelinks-test--fixture-env)
    ;; accept the "gaussain" wrap-span specifically
    (let ((g (seq-find (lambda (s) (string= "gaussain" (notelinks-sug-link-desc s)))
                       notelinks--suggestions)))
      (should g)
      (goto-char (overlay-start (notelinks-sug-overlay g)))
      (notelinks-accept))
    (notelinks-quit)
    (let ((txt (buffer-string)))
      (should (string-match-p
               (regexp-quote "[[id:9E185F19-501A-4C4C-BD7B-8D57105C70AE][gaussain]]") txt))
      ;; nothing else got added
      (should-not (string-match-p "decomposition is the" txt))
      (should-not (string-match-p (regexp-quote "[[id:40628C21") txt)))))

;;;; Synthetic — mode detection & overlap discard

(defun notelinks-test--sug (id conf expect before after &optional template desc)
  "Build a raw suggestion alist for a wrap-span (or insert when EXPECT is empty)."
  `((id . ,id) (type . "analogous-mechanism") (confidence . ,conf)
    (why . ,(concat "why-" id))
    (target_excerpt . "excerpt")
    (target . ((file . "other.org") (title . "Other") (file_id . "FID") (heading)))
    (source_anchor . ((char_start . 0)
                      (char_end . ,(length expect))
                      (expect . ,expect)
                      (before . ,before)
                      (after . ,after)
                      (template . ,(or template "{{link}}"))
                      (link_description . ,(or desc expect))))))

(ert-deftest notelinks-test-mode-detection ()
  (let ((wrap (notelinks--make-sug (notelinks-test--sug "w" 3 "beta" "one " " two")))
        (ins  (notelinks--make-sug (notelinks-test--sug "i" 3 "" "one " "two" "X {{link}} Y" "D"))))
    (should (eq 'wrap (notelinks-sug-mode wrap)))
    (should (eq 'insert (notelinks-sug-mode ins)))))

(ert-deftest notelinks-test-overlap-discards-lower-confidence ()
  ;; buffer: "one two three four"; A wraps "two three" (conf 3),
  ;; B wraps "three four" (conf 5); they overlap on "three" -> keep B.
  (let ((env `((version . 1)
               (source . ((file) (title . "x") (id . "SID")))
               (suggestions . (,(notelinks-test--sug "A" 1 "two three" "one " " four")
                               ,(notelinks-test--sug "B" 3 "three four" "two " ""))))))
    (notelinks-test--with-review "one two three four\n" env
      (should (= 1 (length notelinks--suggestions)))
      (should (string= "three four" (notelinks-sug-link-desc (car notelinks--suggestions)))))))

;;;; Co-located inserts (multiple inserts at the same anchor)

(defun notelinks-test--coinsert-env ()
  "Two inserts anchored at the same point (before \"beta\"): templates ONE/TWO."
  `((version . 1)
    (source . ((file) (title . "x") (id . "SID")))
    (suggestions . (,(notelinks-test--sug "A" 3 "" "alpha " "beta" "ONE" "dA")
                    ,(notelinks-test--sug "B" 3 "" "alpha " "beta" "TWO" "dB")))))

(ert-deftest notelinks-test-coinserts-laid-out-side-by-side ()
  ;; Both inserts survive (zero-width → not overlapping) and are placed in a
  ;; single space-separated run, each with its own overlay.
  (notelinks-test--with-review "alpha beta\n" (notelinks-test--coinsert-env)
    (should (= 2 (length notelinks--suggestions)))
    (let ((txt (buffer-string)))
      (should (string-match-p "alpha \\(ONE TWO\\|TWO ONE\\) beta" txt))
      (should-not (string-match-p "  " txt)))          ; never a double space
    ;; the two overlays are distinct and adjacent (separator owned by the 2nd),
    ;; never stacked on top of each other.
    (let* ((ovs (sort (mapcar #'notelinks-sug-overlay notelinks--suggestions)
                      (lambda (a b) (< (overlay-start a) (overlay-start b)))))
           (a (car ovs)) (b (cadr ovs)))
      (should (= (overlay-end a) (overlay-start b))))))

(ert-deftest notelinks-test-coinserts-reject-all-restores-buffer ()
  (let ((content "alpha beta\n"))
    (notelinks-test--with-review content (notelinks-test--coinsert-env)
      (should (string-match-p "ONE" (buffer-string)))
      (notelinks-quit)
      (should (string= content (buffer-string))))))

(ert-deftest notelinks-test-coinserts-reject-one-keeps-single-space ()
  ;; Rejecting one co-located insert collapses cleanly (its overlay owns the
  ;; adjacent separator), leaving the other and no stray double space.
  (notelinks-test--with-review "alpha beta\n" (notelinks-test--coinsert-env)
    (let ((b (seq-find (lambda (s) (string= "dB" (notelinks-sug-link-desc s)))
                       notelinks--suggestions)))
      (goto-char (overlay-start (notelinks-sug-overlay b)))
      (notelinks-reject))
    (let ((txt (buffer-string)))
      (should (string-match-p "alpha ONE beta" txt))
      (should-not (string-match-p "TWO" txt))
      (should-not (string-match-p "  " txt)))))

(ert-deftest notelinks-test-min-confidence-filters ()
  ;; buffer: "one two three four"; A wraps "two" (conf 1), B wraps "four" (conf 3).
  ;; With the default threshold (2) only B survives; lowering it keeps both.
  (let ((env `((version . 1)
               (source . ((file) (title . "x") (id . "SID")))
               (suggestions . (,(notelinks-test--sug "A" 1 "two" "one " " three")
                               ,(notelinks-test--sug "B" 3 "four" "three " ""))))))
    (let ((notelinks-min-confidence 2))
      (notelinks-test--with-review "one two three four\n" env
        (should (= 1 (length notelinks--suggestions)))
        (should (string= "four" (notelinks-sug-link-desc (car notelinks--suggestions))))))
    (let ((notelinks-min-confidence 1))
      (notelinks-test--with-review "one two three four\n" env
        (should (= 2 (length notelinks--suggestions)))))))

;;;; Keybindings & jump

(ert-deftest notelinks-test-jump-bound ()
  (should (eq 'notelinks-jump-to-target (lookup-key notelinks-overlay-map "j")))
  (should (eq 'notelinks-jump-to-target (lookup-key notelinks-review-mode-map (kbd "C-c C-j")))))

(ert-deftest notelinks-test-quit-bound ()
  ;; C-g quits buffer-wide (review map) and from the panel, alongside q.
  (should (eq 'notelinks-quit (lookup-key notelinks-review-mode-map (kbd "C-g"))))
  (should (eq 'notelinks-quit (lookup-key notelinks-review-mode-map (kbd "C-c C-q"))))
  (should (eq 'notelinks-panel-quit (lookup-key notelinks-panel-mode-map (kbd "C-g"))))
  (should (eq 'notelinks-panel-quit (lookup-key notelinks-panel-mode-map "q"))))

(ert-deftest notelinks-test-jump-errors-off-overlay ()
  (with-temp-buffer
    (org-mode)
    (insert "no suggestions here\n")
    (goto-char (point-min))
    (should-error (notelinks-jump-to-target) :type 'user-error)))

(ert-deftest notelinks-test-refresh-info-is-safe ()
  "Navigating (which refreshes the info panel) must not error."
  (notelinks-test--with-review (notelinks-test--read "epistemic_uncertainty.org")
      (notelinks-test--fixture-env)
    (notelinks--goto-first)
    (notelinks-next)
    (notelinks-prev)
    (should (notelinks--at-point))))

;;;; Info panel

(ert-deftest notelinks-test-info-describes-suggestion ()
  ;; Target info now lives in the posframe; its content comes from --describe.
  (let* ((s (notelinks--make-sug
             (notelinks-test--sug "p" 3 "beta" "one " " two")))
         (txt (notelinks--describe s)))
    (should (string-match-p "analogous-mechanism" txt))     ; type
    (should (string-match-p "★3" txt))                      ; confidence
    (should (string-match-p "why-p" txt))                   ; why
    (should (string-match-p "Other" txt))))                 ; target title

(ert-deftest notelinks-test-panel-shows-key-legend ()
  ;; The bottom side panel now carries only the static key legend.
  (should (string-match-p "a accept" notelinks--panel-keys))
  (should (string-match-p "C-g" notelinks--panel-keys))
  (should (string-match-p "quit" notelinks--panel-keys)))

(ert-deftest notelinks-test-panel-q-bound ()
  (should (eq 'notelinks-panel-quit (lookup-key notelinks-panel-mode-map "q"))))

(ert-deftest notelinks-test-panel-q-quits-review ()
  "Pressing q in the panel ends the review and kills the panel buffer."
  (let ((content (notelinks-test--read "epistemic_uncertainty.org"))
        (env (notelinks-test--fixture-env)))
    (with-temp-buffer
      (insert content)
      (org-mode)
      (let ((src (current-buffer)))
        (cl-letf (((symbol-function 'notelinks--report) (lambda (&rest _) nil))
                  ;; avoid batch windowing fragility; panel buffer is still made
                  ((symbol-function 'display-buffer-in-side-window) (lambda (&rest _) nil)))
          (notelinks--on-result src env))
        (should notelinks--suggestions)
        (let ((panel (get-buffer notelinks--panel-buffer-name)))
          (should (buffer-live-p panel))
          (with-current-buffer panel
            (should (eq notelinks--panel-source src))
            (should (derived-mode-p 'notelinks-panel-mode))
            (notelinks-panel-quit))
          (should-not (buffer-live-p panel)))          ; panel killed
        (should-not notelinks--suggestions)            ; review ended
        (should-not notelinks-review-mode)))))

;;;; HTTP backend

(ert-deftest notelinks-test-http-body-extracts-decoded-body ()
  (with-temp-buffer
    (set-buffer-multibyte nil)
    (insert "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
    (insert (encode-coding-string "{\"note\":\"café\"}" 'utf-8))
    (should (string= "{\"note\":\"café\"}" (notelinks--http-body)))))

(ert-deftest notelinks-test-http-request-body-roundtrips ()
  (let* ((body (json-encode `((buffer . ,"café — prediction error"))))
         (parsed (json-parse-string body :object-type 'alist)))
    (should (string= "café — prediction error" (alist-get 'buffer parsed)))))

(ert-deftest notelinks-test-http-callback-routes-to-review ()
  "A simulated 200 response drives the same review pipeline as the CLI."
  (let ((json (notelinks-test--read "sample_output.json"))
        (content (notelinks-test--read "epistemic_uncertainty.org"))
        (notelinks-min-confidence 1))   ; keep all four fixture suggestions
    (with-temp-buffer
      (insert content)
      (org-mode)
      (let ((src (current-buffer))
            (http (generate-new-buffer " *notelinks-test-http*")))
        (with-current-buffer http
          (set-buffer-multibyte nil)
          (setq-local url-http-response-status 200)
          (insert "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
          (insert (encode-coding-string json 'utf-8)))
        (cl-letf (((symbol-function 'notelinks--show-panel) #'ignore)
                  ((symbol-function 'notelinks--report) (lambda (&rest _) nil)))
          (with-current-buffer http
            (notelinks--http-callback nil src)))   ; status nil ⇒ no :error
        (should-not (buffer-live-p http))           ; callback cleans up its buffer
        (should (= 4 (length notelinks--suggestions)))))))

(ert-deftest notelinks-test-http-callback-reports-non-2xx ()
  (let ((src (get-buffer-create " *notelinks-test-src*"))
        (http (generate-new-buffer " *notelinks-test-http*"))
        (failed nil))
    (with-current-buffer http
      (set-buffer-multibyte nil)
      (setq-local url-http-response-status 500)
      (insert "HTTP/1.1 500 Internal Server Error\r\n\r\nboom"))
    (cl-letf (((symbol-function 'notelinks--fail)
               (lambda (title _detail) (setq failed title))))
      (with-current-buffer http (notelinks--http-callback nil src)))
    (should (string-match-p "HTTP 500" failed))
    (kill-buffer src)))

(ert-deftest notelinks-test-backend-default-and-validation ()
  (should (eq 'cli notelinks-backend))
  (with-temp-buffer
    (org-mode)
    (let ((notelinks-backend 'bogus))
      (should-error (notelinks-suggest) :type 'user-error))))

(provide 'notelinks-test)
;;; notelinks-test.el ends here
