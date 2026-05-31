;;; notelinks.el --- Review suggested org-roam links inline -*- lexical-binding: t; -*-

;; Author: notelinks
;; Keywords: outlines, convenience, org
;; Package-Requires: ((emacs "27.1"))

;;; Commentary:

;; Frontend for the notelinks engine.  `notelinks-suggest' sends the current
;; org buffer to the engine over stdin, then renders the returned suggestions
;; (see docs/json-format.md) as inline overlays in the buffer.  Review is
;; "contextual-modal": single keys (a/r/n/p/q) act while point is inside a
;; suggestion overlay; elsewhere editing is free.  C-c C-n / C-c C-p / C-c C-q
;; work anywhere during a review session.
;;
;; Accept finalizes a suggestion (wrap a span into an `[[id:...]]' link, or keep
;; inserted prose); reject reverts it.  Positions are resolved once against the
;; live buffer via the engine's expect/before/after text and then tracked with
;; markers/overlays, so accepting one suggestion never breaks the others.

;;; Code:

(require 'cl-lib)
(require 'seq)
(require 'subr-x)
(require 'json)

;;;; Customization

(defgroup notelinks nil
  "Review suggested org-roam links inline."
  :group 'org
  :prefix "notelinks-")

(defcustom notelinks-command '("notelinks")
  "Engine command as a list: program followed by leading arguments.
For example `(\"uv\" \"run\" \"notelinks\")'."
  :type '(repeat string))

(defcustom notelinks-corpus-dir nil
  "Corpus root passed to the engine as `--corpus'.
When nil the engine falls back to its own NOTELINKS_CORPUS_DIR."
  :type '(choice (const :tag "Engine default" nil) directory))

(defcustom notelinks-navigation-order 'buffer
  "Order in which `notelinks-next'/`notelinks-prev' walk suggestions."
  :type '(choice (const :tag "Buffer order" buffer)
                 (const :tag "Confidence order" confidence)))

(defface notelinks-suggestion
  '((t :inherit highlight))
  "Face marking a pending suggestion region in the buffer.")

;;;; State (buffer-local in the source note buffer)

(cl-defstruct notelinks-sug
  id type confidence why source-chunk target-excerpt target anchor
  link-desc template mode overlay)

(defvar-local notelinks--suggestions nil
  "List of live `notelinks-sug' structs being reviewed in this buffer.")

(defvar-local notelinks--legend-window nil
  "Side window showing the key legend during a review session.")

;; Forward declarations (defined fully near the bottom of the file).
(defvar notelinks-overlay-map)
(defvar notelinks-review-mode)
(declare-function org-link-open-from-string "ol" (s &optional arg))

;;;; Engine invocation

(defun notelinks--corpus-dir ()
  "Return the configured corpus directory (expanded) or nil."
  (let ((d (or notelinks-corpus-dir (getenv "NOTELINKS_CORPUS_DIR"))))
    (and d (expand-file-name d))))

(defun notelinks--set-status (buf str)
  (when (buffer-live-p buf)
    (with-current-buffer buf (setq-local mode-line-process str) (force-mode-line-update))))

(defun notelinks--clear-status (buf)
  (when (buffer-live-p buf)
    (with-current-buffer buf (setq-local mode-line-process nil) (force-mode-line-update))))

;;;###autoload
(defun notelinks-suggest ()
  "Query the engine for link suggestions on the current buffer and review them."
  (interactive)
  (unless (derived-mode-p 'org-mode)
    (user-error "notelinks: not an Org buffer"))
  (when notelinks--suggestions
    (user-error "notelinks: a review is already in progress (quit it first)"))
  (let* ((src (current-buffer))
         (text (buffer-substring-no-properties (point-min) (point-max)))
         (corpus (notelinks--corpus-dir))
         (program (car notelinks-command))
         (args (append (cdr notelinks-command)
                       (list "suggest")
                       (when corpus (list "--corpus" corpus))))
         (stdout (generate-new-buffer " *notelinks-stdout*"))
         (stderr (generate-new-buffer " *notelinks-stderr*"))
         (proc (make-process
                :name "notelinks"
                :command (cons program args)
                :buffer stdout
                :stderr stderr
                :connection-type 'pipe
                :noquery t
                :sentinel #'notelinks--sentinel)))
    (process-put proc 'notelinks-src src)
    (process-put proc 'notelinks-stdout stdout)
    (process-put proc 'notelinks-stderr stderr)
    (process-send-string proc text)
    (process-send-eof proc)
    (notelinks--set-status src " ⟳notelinks")
    (message "notelinks: querying corpus…")))

(defun notelinks--sentinel (proc _event)
  (when (memq (process-status proc) '(exit signal))
    (let* ((src (process-get proc 'notelinks-src))
           (stdout (process-get proc 'notelinks-stdout))
           (stderr (process-get proc 'notelinks-stderr))
           (code (process-exit-status proc)))
      (notelinks--clear-status src)
      (unwind-protect
          (cond
           ((not (buffer-live-p src))
            (message "notelinks: source buffer gone, discarding result"))
           ((zerop code)
            (notelinks--handle-json src (with-current-buffer stdout (buffer-string)) stderr))
           (t (notelinks--show-error stderr code nil)))
        (when (buffer-live-p stdout) (kill-buffer stdout))
        (when (buffer-live-p stderr) (kill-buffer stderr))))))

(defun notelinks--handle-json (src json stderr)
  (let ((env (condition-case nil
                 (json-parse-string json :object-type 'alist :array-type 'list
                                    :null-object nil :false-object nil)
               (error nil))))
    (if env
        (notelinks--on-result src env)
      (notelinks--show-error stderr 0 json))))

(defun notelinks--show-error (stderr code json)
  (let ((buf (get-buffer-create "*notelinks-error*"))
        (err (and (buffer-live-p stderr) (with-current-buffer stderr (buffer-string)))))
    (with-current-buffer buf
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert (format "notelinks engine error (exit %s)\n\n" code))
        (when (and err (not (string-empty-p err))) (insert "stderr:\n" err "\n"))
        (when (and json (not (string-empty-p json))) (insert "\nstdout (not valid JSON):\n" json)))
      (special-mode))
    (display-buffer buf)
    (message "notelinks: engine error (see *notelinks-error*)")))

;;;; Link assembly & template filling

(defun notelinks--assemble-link (target desc)
  "Build an org `[[id:...][DESC]]' link from TARGET components."
  (let* ((file-id (alist-get 'file_id target))
         (heading (alist-get 'heading target))
         (heading-id (and heading (alist-get 'id heading)))
         (heading-text (and heading (alist-get 'text heading)))
         (path (cond
                ((null heading) (format "id:%s" file-id))
                (heading-id (format "id:%s" heading-id))
                (t (format "id:%s::*%s" file-id heading-text)))))
    (format "[[%s][%s]]" path desc)))

(defun notelinks--fill (template link)
  "Substitute LINK for the {{link}} slot in TEMPLATE."
  (replace-regexp-in-string (regexp-quote "{{link}}") link (or template "{{link}}") t t))

;;;; Anchor resolution

(defun notelinks--find-near (needle guess)
  "Return buffer position of the occurrence of NEEDLE closest to GUESS, or nil."
  (when (and needle (> (length needle) 0))
    (save-excursion
      (let (positions)
        (goto-char (point-min))
        (while (search-forward needle nil t)
          (push (match-beginning 0) positions))
        (when positions
          (car (sort positions (lambda (a b) (< (abs (- a guess)) (abs (- b guess)))))))))))

(defun notelinks--resolve (s)
  "Locate suggestion S in the current buffer.
Return a cons (BEG . END) of buffer positions (BEG=END for an insert),
or nil if it cannot be confidently anchored."
  (let* ((anchor (notelinks-sug-anchor s))
         (before (or (alist-get 'before anchor) ""))
         (expect (or (alist-get 'expect anchor) ""))
         (after (or (alist-get 'after anchor) ""))
         (cs (or (alist-get 'char_start anchor) 0))
         (needle (concat before expect after))
         ;; the needle starts `before' chars ahead of the span start
         (guess (+ (point-min) (max 0 (- cs (length before)))))
         (mb (notelinks--find-near needle guess)))
    (when mb
      (let ((beg (+ mb (length before))))
        (cons beg (+ beg (length expect)))))))

;;;; Overlap filtering (defensive — engine is expected to avoid overlaps)

(defun notelinks--overlap-p (a b)
  "Non-nil if resolved plists A and B occupy overlapping buffer ranges."
  (let ((ab (plist-get a :beg)) (ae (plist-get a :end))
        (bb (plist-get b :beg)) (be (plist-get b :end)))
    (and (< ab be) (< bb ae))))

(defun notelinks--filter-overlaps (resolved)
  "Greedily keep the highest-confidence non-overlapping suggestions.
RESOLVED is a list of plists (:sug :beg :end).  Return (KEPT . DISCARDED)
where KEPT is plists and DISCARDED is `notelinks-sug' structs."
  (let ((sorted (sort (copy-sequence resolved)
                      (lambda (a b)
                        (> (notelinks-sug-confidence (plist-get a :sug))
                           (notelinks-sug-confidence (plist-get b :sug))))))
        kept discarded)
    (dolist (r sorted)
      (if (cl-some (lambda (k) (notelinks--overlap-p r k)) kept)
          (push (plist-get r :sug) discarded)
        (push r kept)))
    (cons (nreverse kept) (nreverse discarded))))

;;;; Building suggestions & overlays

(defun notelinks--make-sug (raw)
  (let* ((anchor (alist-get 'source_anchor raw))
         (cs (alist-get 'char_start anchor))
         (ce (alist-get 'char_end anchor))
         (mode (if (and (numberp cs) (numberp ce) (> ce cs)) 'wrap 'insert)))
    (make-notelinks-sug
     :id (alist-get 'id raw)
     :type (alist-get 'type raw)
     :confidence (or (alist-get 'confidence raw) 0)
     :why (alist-get 'why raw)
     :source-chunk (alist-get 'source_chunk raw)
     :target-excerpt (alist-get 'target_excerpt raw)
     :target (alist-get 'target raw)
     :anchor anchor
     :link-desc (alist-get 'link_description anchor)
     :template (alist-get 'template anchor)
     :mode mode)))

(defun notelinks--make-overlay (s beg end)
  (let ((ov (make-overlay beg end nil nil nil)))
    (overlay-put ov 'notelinks-sug s)
    (overlay-put ov 'face 'notelinks-suggestion)
    (overlay-put ov 'keymap notelinks-overlay-map)
    (overlay-put ov 'help-echo #'notelinks--help-echo)
    (setf (notelinks-sug-overlay s) ov)
    ov))

(defun notelinks--on-result (src env)
  (with-current-buffer src
    (let* ((raws (alist-get 'suggestions env))
           (sugs (mapcar #'notelinks--make-sug raws))
           resolved unanchorable)
      ;; Phase 1: resolve positions (read-only).
      (dolist (s sugs)
        (let ((pos (notelinks--resolve s)))
          (if pos
              (push (list :sug s :beg (car pos) :end (cdr pos)) resolved)
            (push s unanchorable))))
      (setq resolved (nreverse resolved)
            unanchorable (nreverse unanchorable))
      ;; Phase 2: discard overlapping lower-confidence suggestions.
      (pcase-let ((`(,kept . ,discarded) (notelinks--filter-overlaps resolved)))
        ;; Phase 3a: pin every kept range with markers before any edit.
        (let (marked)
          (dolist (r kept)
            (let* ((s (plist-get r :sug))
                   (insert? (eq (notelinks-sug-mode s) 'insert))
                   ;; beg stays before inserted text; end advances only for
                   ;; inserts (so the new text lands inside the region).
                   (tbm (copy-marker (plist-get r :beg) nil))
                   (tem (copy-marker (plist-get r :end) (and insert? t))))
              (push (list s tbm tem) marked)))
          (setq marked (nreverse marked))
          ;; Phase 3b: perform insertions (markers absorb the shifts).
          (dolist (m marked)
            (let ((s (nth 0 m)) (tbm (nth 1 m)))
              (when (eq (notelinks-sug-mode s) 'insert)
                (let* ((link (notelinks--assemble-link
                              (notelinks-sug-target s) (or (notelinks-sug-link-desc s) "")))
                       (text (notelinks--fill (notelinks-sug-template s) link)))
                  (save-excursion (goto-char tbm) (insert text))))))
          ;; Phase 4: build overlays from final marker positions.
          (dolist (m marked)
            (let ((s (nth 0 m)) (tbm (nth 1 m)) (tem (nth 2 m)))
              (notelinks--make-overlay s (marker-position tbm) (marker-position tem))
              (set-marker tbm nil) (set-marker tem nil)))
          (setq notelinks--suggestions (mapcar #'car marked)))
        ;; Report & enter review.
        (notelinks--report notelinks--suggestions unanchorable discarded)
        (if (null notelinks--suggestions)
            (message "notelinks: no applicable suggestions")
          (notelinks-review-mode 1)
          (notelinks--show-legend)
          (notelinks--goto-first))))))

(defun notelinks--report (kept unanchorable discarded)
  (when (or unanchorable discarded)
    (let ((buf (get-buffer-create "*notelinks*")))
      (with-current-buffer buf
        (let ((inhibit-read-only t))
          (erase-buffer)
          (when unanchorable
            (insert (format "Unanchorable (%d):\n" (length unanchorable)))
            (dolist (s unanchorable)
              (insert (format "  - [%s] %s\n" (notelinks-sug-type s) (notelinks-sug-why s))))
            (insert "\n"))
          (when discarded
            (insert (format "Discarded — overlap (%d):\n" (length discarded)))
            (dolist (s discarded)
              (insert (format "  - [%s] %s\n" (notelinks-sug-type s) (notelinks-sug-why s))))))
        (special-mode))
      (display-buffer buf)))
  (message "notelinks: %d shown%s%s"
           (length kept)
           (if unanchorable (format ", %d unanchorable" (length unanchorable)) "")
           (if discarded (format ", %d discarded" (length discarded)) "")))

;;;; Metainfo (eldoc + help-echo)

(defun notelinks--describe (s)
  (let* ((tgt (notelinks-sug-target s))
         (title (alist-get 'title tgt))
         (heading (alist-get 'heading tgt))
         (htext (and heading (alist-get 'text heading)))
         (loc (if htext (format "%s › %s" title htext) title))
         (excerpt (notelinks-sug-target-excerpt s)))
    (concat (format "[%s ★%d] %s" (notelinks-sug-type s) (notelinks-sug-confidence s)
                    (notelinks-sug-why s))
            (format "\n→ %s" loc)
            (when (and excerpt (not (string-empty-p excerpt)))
              (format "\n  \"%s\"" excerpt)))))

(defun notelinks--help-echo (_window object _pos)
  (let ((s (overlay-get object 'notelinks-sug)))
    (and s (notelinks--describe s))))

(defun notelinks--eldoc (&rest _)
  (let ((s (notelinks--at-point)))
    (and s (notelinks--describe s))))

;;;; Navigation

(defun notelinks--ov-at (pos)
  (seq-some (lambda (ov) (overlay-get ov 'notelinks-sug)) (overlays-at pos)))

(defun notelinks--at-point ()
  "Return the `notelinks-sug' whose overlay covers point, or nil."
  (or (notelinks--ov-at (point))
      (and (> (point) (point-min)) (notelinks--ov-at (1- (point))))))

(defun notelinks--ordered ()
  (let ((l (copy-sequence notelinks--suggestions)))
    (if (eq notelinks-navigation-order 'confidence)
        (sort l (lambda (a b) (> (notelinks-sug-confidence a) (notelinks-sug-confidence b))))
      (sort l (lambda (a b) (< (overlay-start (notelinks-sug-overlay a))
                               (overlay-start (notelinks-sug-overlay b))))))))

(defun notelinks--refresh-info ()
  "Surface the metainfo for the suggestion at point immediately.
Forces an eldoc refresh so the popup updates the instant a navigation
command (n/p/j/accept) lands point on a suggestion, rather than waiting
for the idle timer."
  (cond
   ((and (bound-and-true-p eldoc-mode) (commandp 'eldoc))
    (ignore-errors (eldoc t)))
   (t (let ((s (notelinks--at-point)))
        (when s (message "%s" (notelinks--describe s)))))))

(defun notelinks--goto (s)
  (goto-char (overlay-start (notelinks-sug-overlay s)))
  (notelinks--refresh-info))

(defun notelinks--goto-first ()
  (let ((o (notelinks--ordered)))
    (when o (notelinks--goto (car o)))))

(defun notelinks-next ()
  "Move to the next suggestion."
  (interactive)
  (let* ((ordered (notelinks--ordered))
         (cur (notelinks--at-point))
         (rest (and cur (cdr (memq cur ordered))))
         (target (or (car rest) (car ordered))))
    (when target (notelinks--goto target))))

(defun notelinks-prev ()
  "Move to the previous suggestion."
  (interactive)
  (let* ((ordered (nreverse (notelinks--ordered)))
         (cur (notelinks--at-point))
         (rest (and cur (cdr (memq cur ordered))))
         (target (or (car rest) (car ordered))))
    (when target (notelinks--goto target))))

(defun notelinks-jump-to-target ()
  "Follow the suggestion at point to its target note/heading."
  (interactive)
  (let ((s (notelinks--at-point)))
    (unless s (user-error "notelinks: no suggestion at point"))
    (require 'ol)
    (let* ((target (notelinks-sug-target s))
           (link (notelinks--assemble-link target (or (alist-get 'title target) ""))))
      (org-link-open-from-string link))))

;;;; Accept / reject / quit

(defun notelinks--apply-accept (s)
  (when (eq (notelinks-sug-mode s) 'wrap)
    (let* ((ov (notelinks-sug-overlay s))
           (beg (overlay-start ov)) (end (overlay-end ov))
           (desc (buffer-substring-no-properties beg end))
           (link (notelinks--assemble-link (notelinks-sug-target s) desc)))
      (save-excursion
        (goto-char beg)
        (delete-region beg end)
        (insert link))))
  ;; insert mode: prose is already in the buffer — keep it as-is.
  )

(defun notelinks--apply-reject (s)
  (when (eq (notelinks-sug-mode s) 'insert)
    (let ((ov (notelinks-sug-overlay s)))
      (delete-region (overlay-start ov) (overlay-end ov))))
  ;; wrap mode: leave the original text untouched.
  )

(defun notelinks--dispose (s)
  (when (notelinks-sug-overlay s)
    (delete-overlay (notelinks-sug-overlay s))
    (setf (notelinks-sug-overlay s) nil))
  (setq notelinks--suggestions (delq s notelinks--suggestions)))

(defun notelinks--after-action ()
  (if (null notelinks--suggestions)
      (notelinks-finish)
    (let* ((ordered (notelinks--ordered))
           (nxt (or (seq-find (lambda (s) (>= (overlay-start (notelinks-sug-overlay s)) (point)))
                              ordered)
                    (car ordered))))
      (notelinks--goto nxt))))

(defun notelinks-accept ()
  "Accept the suggestion at point and advance."
  (interactive)
  (let ((s (notelinks--at-point)))
    (unless s (user-error "notelinks: no suggestion at point"))
    (notelinks--apply-accept s)
    (notelinks--dispose s)
    (notelinks--after-action)))

(defun notelinks-reject ()
  "Reject the suggestion at point and advance."
  (interactive)
  (let ((s (notelinks--at-point)))
    (unless s (user-error "notelinks: no suggestion at point"))
    (notelinks--apply-reject s)
    (notelinks--dispose s)
    (notelinks--after-action)))

(defun notelinks-quit ()
  "Reject all remaining suggestions and end the review."
  (interactive)
  (dolist (s (copy-sequence notelinks--suggestions))
    (notelinks--apply-reject s)
    (when (notelinks-sug-overlay s)
      (delete-overlay (notelinks-sug-overlay s))
      (setf (notelinks-sug-overlay s) nil)))
  (setq notelinks--suggestions nil)
  (notelinks-finish))

(defun notelinks-finish ()
  "End the review session, clearing any remaining overlays."
  (interactive)
  (dolist (s notelinks--suggestions)
    (when (notelinks-sug-overlay s) (delete-overlay (notelinks-sug-overlay s))))
  (setq notelinks--suggestions nil)
  (notelinks--hide-legend)
  (when notelinks-review-mode (notelinks-review-mode -1))
  (message "notelinks: review done"))

;;;; Legend

(defun notelinks--legend-text ()
  "notelinks review — keys (active while point is on a suggestion):
  a accept   r reject   n next   p previous   j jump to target   q quit
elsewhere: C-c C-n next   C-c C-p prev   C-c C-j jump   C-c C-q quit")

(defun notelinks--show-legend ()
  (let ((buf (get-buffer-create "*notelinks-keys*")))
    (with-current-buffer buf
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert (notelinks--legend-text)))
      (setq buffer-read-only t)
      (setq-local mode-line-format nil))
    (setq notelinks--legend-window
          (display-buffer-in-side-window buf '((side . bottom) (window-height . 4))))))

(defun notelinks--hide-legend ()
  (when (window-live-p notelinks--legend-window)
    (delete-window notelinks--legend-window))
  (setq notelinks--legend-window nil)
  (when-let ((buf (get-buffer "*notelinks-keys*")))
    (kill-buffer buf)))

;;;; Keymaps & review mode

(defvar notelinks-overlay-map
  (let ((m (make-sparse-keymap)))
    (define-key m "a" #'notelinks-accept)
    (define-key m "r" #'notelinks-reject)
    (define-key m "n" #'notelinks-next)
    (define-key m "p" #'notelinks-prev)
    (define-key m "j" #'notelinks-jump-to-target)
    (define-key m "q" #'notelinks-quit)
    m)
  "Keymap active while point is inside a suggestion overlay.")

(defvar notelinks-review-mode-map
  (let ((m (make-sparse-keymap)))
    (define-key m (kbd "C-c C-n") #'notelinks-next)
    (define-key m (kbd "C-c C-p") #'notelinks-prev)
    (define-key m (kbd "C-c C-j") #'notelinks-jump-to-target)
    (define-key m (kbd "C-c C-q") #'notelinks-quit)
    m)
  "Keymap active buffer-wide during a notelinks review session.")

(define-minor-mode notelinks-review-mode
  "Minor mode active while reviewing notelinks suggestions."
  :lighter " NoteLinks"
  :keymap notelinks-review-mode-map
  (if notelinks-review-mode
      (progn
        (if (boundp 'eldoc-documentation-functions)
            (add-hook 'eldoc-documentation-functions #'notelinks--eldoc nil t)
          (setq-local eldoc-documentation-function #'notelinks--eldoc))
        (eldoc-mode 1))
    (if (boundp 'eldoc-documentation-functions)
        (remove-hook 'eldoc-documentation-functions #'notelinks--eldoc t)
      (kill-local-variable 'eldoc-documentation-function))))

(provide 'notelinks)
;;; notelinks.el ends here
