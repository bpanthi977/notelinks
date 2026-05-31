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
       (cl-letf (((symbol-function 'notelinks--show-legend) #'ignore)
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

;;;; Unit tests — template filling

(ert-deftest notelinks-test-fill-substitutes-link ()
  (should (equal "see [[id:x][y]] now"
                 (notelinks--fill "see {{link}} now" "[[id:x][y]]"))))

(ert-deftest notelinks-test-fill-default-template ()
  (should (equal "[[id:x][y]]" (notelinks--fill nil "[[id:x][y]]"))))

;;;; Fixture-driven — resolution, accept, reject

(ert-deftest notelinks-test-resolves-all-fixture-suggestions ()
  (notelinks-test--with-review (notelinks-test--read "epistemic_uncertainty.org")
      (notelinks-test--fixture-env)
    (should (= 4 (length notelinks--suggestions)))))

(ert-deftest notelinks-test-accept-all-produces-correct-links ()
  (notelinks-test--with-review (notelinks-test--read "epistemic_uncertainty.org")
      (notelinks-test--fixture-env)
    (notelinks-test--accept-all)
    (should (= 0 (length notelinks--suggestions)))
    (let ((txt (buffer-string)))
      ;; insert anchor -> heading-with-id link inside engine-authored prose
      (should (string-match-p
               (regexp-quote "[[id:6398DC98-3FD0-45B5-B2CA-E0D8E81F5583][Mutual Information]]") txt))
      ;; wrap-span -> heading-with-id
      (should (string-match-p
               (regexp-quote "[[id:9E185F19-501A-4C4C-BD7B-8D57105C70AE][gaussain]]") txt))
      ;; wrap-span -> file-level (mention)
      (should (string-match-p
               (regexp-quote "[[id:40628C21-A838-45DA-836C-2FA6E9F3B4E6][entropy of the mixture]]") txt))
      ;; wrap-span -> no-id heading fallback (::*Heading)
      (should (string-match-p
               (regexp-quote "[[id:40628C21-A838-45DA-836C-2FA6E9F3B4E6::*Statistical Mechanics][averaged over all models]]")
               txt)))))

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
    (source_chunk . ((text . "") (heading) (char_start . 0) (char_end . 0)))
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
               (suggestions . (,(notelinks-test--sug "A" 3 "two three" "one " " four")
                               ,(notelinks-test--sug "B" 5 "three four" "two " ""))))))
    (notelinks-test--with-review "one two three four\n" env
      (should (= 1 (length notelinks--suggestions)))
      (should (string= "three four" (notelinks-sug-link-desc (car notelinks--suggestions)))))))

;;;; Keybindings & jump

(ert-deftest notelinks-test-jump-bound ()
  (should (eq 'notelinks-jump-to-target (lookup-key notelinks-overlay-map "j")))
  (should (eq 'notelinks-jump-to-target (lookup-key notelinks-review-mode-map (kbd "C-c C-j")))))

(ert-deftest notelinks-test-jump-errors-off-overlay ()
  (with-temp-buffer
    (org-mode)
    (insert "no suggestions here\n")
    (goto-char (point-min))
    (should-error (notelinks-jump-to-target) :type 'user-error)))

(ert-deftest notelinks-test-refresh-info-is-safe ()
  "Navigating (which forces an eldoc refresh) must not error."
  (notelinks-test--with-review (notelinks-test--read "epistemic_uncertainty.org")
      (notelinks-test--fixture-env)
    (notelinks--goto-first)
    (notelinks-next)
    (notelinks-prev)
    (should (notelinks--at-point))))

(provide 'notelinks-test)
;;; notelinks-test.el ends here
