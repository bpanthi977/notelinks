;;; notelinks.el --- Review suggested org-roam links inline -*- lexical-binding: t; -*-

;; Author: notelinks
;; Keywords: outlines, convenience, org
;; Package-Requires: ((emacs "27.1") (posframe "1.1.0"))

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
(require 'url)
(require 'url-http)

;;;; Customization

(defgroup notelinks nil
  "Review suggested org-roam links inline."
  :group 'org
  :prefix "notelinks-")

(defcustom notelinks-backend 'cli
  "How to reach the engine.
`cli'  — spawn `notelinks suggest' per query (cold start + index walk).
`http' — POST to a running `notelinks serve' daemon (warm, fast)."
  :type '(choice (const :tag "CLI subprocess" cli)
                 (const :tag "HTTP daemon" http)))

(defcustom notelinks-command '("notelinks")
  "Engine command as a list: program followed by leading arguments.
For example `(\"uv\" \"run\" \"notelinks\")'.  Used by the `cli' backend."
  :type '(repeat string))

(defcustom notelinks-server-url "http://127.0.0.1:8765"
  "Base URL of the `notelinks serve' daemon.  Used by the `http' backend."
  :type 'string)

(defcustom notelinks-corpus-dir nil
  "Corpus root passed to the engine as `--corpus'.
When nil the engine falls back to its own NOTELINKS_CORPUS_DIR."
  :type '(choice (const :tag "Engine default" nil) directory))

(defcustom notelinks-navigation-order 'buffer
  "Order in which `notelinks-next'/`notelinks-prev' walk suggestions."
  :type '(choice (const :tag "Buffer order" buffer)
                 (const :tag "Confidence order" confidence)))

(defcustom notelinks-min-confidence 2
  "Minimum confidence (1–3) a suggestion must have to be reviewed.
Suggestions the engine returns below this level are dropped before resolution
\(reported as a count, not shown).  Set to 1 to keep everything."
  :type 'integer)

(defface notelinks-suggestion
  '((t :inherit highlight))
  "Face marking a pending wrap suggestion region in the buffer.")

(defface notelinks-insert
  '((t :inherit highlight :box (:line-width -1 :color "green")))
  "Face marking a pending insert suggestion (prose added by the engine).
A green outline distinguishes inserted text from a wrapped span.")

;;;; State (buffer-local in the source note buffer)

(cl-defstruct notelinks-sug
  id type confidence why target-excerpt target anchor
  link-desc template mode overlay)

(defvar-local notelinks--suggestions nil
  "List of live `notelinks-sug' structs being reviewed in this buffer.")

(defvar-local notelinks--info-window nil
  "Bottom side window showing the key legend for the review session.")

(defvar-local notelinks--panel-current nil
  "The suggestion last shown in the info posframe (to avoid needless redraws).")

(defvar-local notelinks--panel-source nil
  "In the panel buffer, the source note buffer whose review it belongs to.")

(defconst notelinks--panel-buffer-name "*notelinks-review*"
  "Name of the buffer shown in the bottom key-legend side window.")

(defconst notelinks--info-buffer-name " *notelinks-info*"
  "Name of the posframe buffer showing the current suggestion's details.")

;; Forward declarations (defined fully near the bottom of the file).
(defvar notelinks-overlay-map)
(defvar notelinks-review-mode)
(declare-function org-link-open-from-string "ol" (s &optional arg))
(declare-function posframe-workable-p "posframe" ())
(declare-function posframe-show "posframe" (buffer &rest args))
(declare-function posframe-hide "posframe" (buffer))
(declare-function posframe-delete "posframe" (buffer))
(declare-function posframe-poshandler-point-bottom-left-corner "posframe" (info))

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
  "Query the engine for link suggestions on the current buffer and review them.
Dispatches on `notelinks-backend' (`cli' subprocess or `http' daemon); both
transports feed the same review pipeline."
  (interactive)
  (unless (derived-mode-p 'org-mode)
    (user-error "notelinks: not an Org buffer"))
  (when notelinks--suggestions
    (user-error "notelinks: a review is already in progress (quit it first)"))
  (let ((src (current-buffer))
        (text (buffer-substring-no-properties (point-min) (point-max))))
    (pcase notelinks-backend
      ('cli (notelinks--start-cli src text))
      ('http (notelinks--start-http src text))
      (other (user-error "notelinks: invalid notelinks-backend %S" other)))
    (notelinks--set-status src " ⟳notelinks")
    (message "notelinks: querying corpus…")))

;;;; Transport: CLI subprocess

(defun notelinks--start-cli (src text)
  (let* ((corpus (notelinks--corpus-dir))
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
    (process-send-eof proc)))

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
            (notelinks--handle-json src (with-current-buffer stdout (buffer-string))))
           (t (notelinks--fail (format "engine exited with %d" code)
                               (with-current-buffer stderr (buffer-string)))))
        (when (buffer-live-p stdout) (kill-buffer stdout))
        (when (buffer-live-p stderr) (kill-buffer stderr))))))

;;;; Transport: HTTP daemon (`notelinks serve')

(defun notelinks--server-url (path)
  (concat (string-trim-right notelinks-server-url "/") path))

(defun notelinks--http-body ()
  "Return the (decoded) response body of the current `url-retrieve' buffer."
  (save-excursion
    (goto-char (point-min))
    (let* ((beg (or (and (boundp 'url-http-end-of-headers)
                         (markerp url-http-end-of-headers)
                         (marker-position url-http-end-of-headers))
                    (and (re-search-forward "\r?\n\r?\n" nil t) (point))
                    (point-min)))
           (s (buffer-substring-no-properties beg (point-max))))
      ;; Real url buffers are unibyte (raw bytes) → decode; test buffers may be
      ;; multibyte already → leave as-is.
      (if (multibyte-string-p s) s (decode-coding-string s 'utf-8)))))

(defun notelinks--start-http (src text)
  (let ((url-request-method "POST")
        (url-request-extra-headers '(("Content-Type" . "application/json")))
        (url-request-data (encode-coding-string
                           (json-encode `((buffer . ,text))) 'utf-8)))
    (url-retrieve (notelinks--server-url "/suggest")
                  #'notelinks--http-callback (list src) t t)))

(defun notelinks--http-callback (status src)
  (let ((http-buf (current-buffer)))
    (unwind-protect
        (progn
          (notelinks--clear-status src)
          (cond
           ((not (buffer-live-p src))
            (message "notelinks: source buffer gone, discarding result"))
           ((plist-get status :error)
            (notelinks--fail (format "cannot reach %s" notelinks-server-url)
                             (format "%S" (plist-get status :error))))
           (t
            (let ((code (and (boundp 'url-http-response-status) url-http-response-status))
                  (body (notelinks--http-body)))
              (if (and (integerp code) (<= 200 code 299))
                  (notelinks--handle-json src body)
                (notelinks--fail (format "HTTP %s from %s" code notelinks-server-url) body))))))
      (when (buffer-live-p http-buf) (kill-buffer http-buf)))))

;;;; Shared: parse & dispatch / errors

(defun notelinks--handle-json (src json)
  (let ((env (condition-case nil
                 (json-parse-string json :object-type 'alist :array-type 'list
                                    :null-object nil :false-object nil)
               (error nil))))
    (if env
        (notelinks--on-result src env)
      (notelinks--fail "engine returned invalid JSON" json))))

(defun notelinks--fail (title detail)
  (let ((buf (get-buffer-create "*notelinks-error*")))
    (with-current-buffer buf
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert "notelinks: " title "\n")
        (when (and detail (not (string-empty-p detail)))
          (insert "\n" detail (if (string-suffix-p "\n" detail) "" "\n"))))
      (special-mode))
    (display-buffer buf))
  (message "notelinks: %s (see *notelinks-error*)" title))

;;;; Server commands (http backend)

(defun notelinks--http-sync (method path &optional body)
  "Send a synchronous request to the daemon; return the parsed JSON alist.
BODY is a JSON string (for POST).  Signals on connection failure or non-2xx."
  (let ((url-request-method method)
        (url-request-extra-headers (and body '(("Content-Type" . "application/json"))))
        (url-request-data (and body (encode-coding-string body 'utf-8)))
        (buf (url-retrieve-synchronously (notelinks--server-url path) t t 10)))
    (unless buf (error "no response from %s" notelinks-server-url))
    (unwind-protect
        (with-current-buffer buf
          (let ((code (and (boundp 'url-http-response-status) url-http-response-status))
                (resp (notelinks--http-body)))
            (unless (and (integerp code) (<= 200 code 299))
              (error "HTTP %s: %s" code resp))
            (json-parse-string resp :object-type 'alist :null-object nil :false-object nil)))
      (when (buffer-live-p buf) (kill-buffer buf)))))

(defun notelinks-server-status ()
  "Show the daemon's index status (notes/chunks/last refresh); confirms it is up."
  (interactive)
  (condition-case e
      (let ((s (notelinks--http-sync "GET" "/status")))
        (message "notelinks daemon: %s notes, %s chunks; last refresh %s"
                 (alist-get 'notes s) (alist-get 'chunks s)
                 (or (alist-get 'last_refresh s) "—")))
    (error (message "notelinks daemon unreachable at %s (%s)"
                    notelinks-server-url (error-message-string e)))))

(defun notelinks-server-refresh (&optional rebuild)
  "Ask the daemon to refresh its index.  With prefix arg, force a full REBUILD."
  (interactive "P")
  (condition-case e
      (let ((s (notelinks--http-sync
                "POST" "/refresh"
                (if rebuild "{\"rebuild\": true}" "{\"rebuild\": false}"))))
        (message "notelinks daemon refreshed: %s" s))
    (error (message "notelinks refresh failed: %s" (error-message-string e)))))

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

(defun notelinks--heading-text (target)
  "Heading text of TARGET when it points at a heading (not file-level), else nil."
  (let ((heading (alist-get 'heading target)))
    (and heading (alist-get 'text heading))))

(defconst notelinks--whitespace '(?\s ?\t ?\n)
  "Characters treated as whitespace when spacing inserted prose.")

(defun notelinks--insert-text (s)
  "Return the prose to insert for insert-suggestion S (its filled template).
When the target is a heading, the heading's text is used as the link
description; otherwise the engine's `link_description' is used."
  (let* ((target (notelinks-sug-target s))
         (desc (or (notelinks--heading-text target)
                   (notelinks-sug-link-desc s)
                   "")))
    (notelinks--fill (notelinks-sug-template s)
                     (notelinks--assemble-link target desc))))

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
     :target-excerpt (alist-get 'target_excerpt raw)
     :target (alist-get 'target raw)
     :anchor anchor
     :link-desc (alist-get 'link_description anchor)
     :template (alist-get 'template anchor)
     :mode mode)))

(defun notelinks--make-overlay (s beg end)
  (let ((ov (make-overlay beg end nil nil nil)))
    (overlay-put ov 'notelinks-sug s)
    (overlay-put ov 'face (if (eq (notelinks-sug-mode s) 'insert)
                              'notelinks-insert
                            'notelinks-suggestion))
    (overlay-put ov 'keymap notelinks-overlay-map)
    (setf (notelinks-sug-overlay s) ov)
    ov))

(defun notelinks--insert-group (marker sugs)
  "Insert the insert-suggestions SUGS at MARKER, in order, and overlay each.
Co-located inserts (the engine emitted several at the same point) are laid out
left-to-right separated by a single space, and the whole run is padded from the
surrounding text by a single space wherever it would otherwise abut non-space.
Spacing lives *inside* the overlays — each overlay owns the separator that
*follows* its text (the first also owns the leading pad) — so the overlays tile
the entire inserted region (a full reject restores the buffer exactly) and
rejecting any one collapses to a single space (the survivors keep their own
trailing separators).  MARKER is an insertion-type-nil marker at the anchor."
  (let* ((wsp notelinks--whitespace)
         (texts (mapcar #'notelinks--insert-text sugs))
         (n (length texts))
         (p (marker-position marker))
         (before (char-before p))
         (after (char-after p))
         (first (car texts))
         (last (car (last texts)))
         (lead (and before (not (memq before wsp))
                    (> (length first) 0) (not (memq (aref first 0) wsp))))
         (trail (and after (not (memq after wsp))
                     (> (length last) 0) (not (memq (aref last (1- (length last))) wsp))))
         (chunks nil)                   ; reversed string parts
         (len 0)
         (spans nil))                   ; reversed list of (sug start . end), relative to P
    (cl-loop
     for text in texts for s in sugs for i from 0 do
     (let ((start len))
       (when (and (zerop i) lead) (push " " chunks) (cl-incf len))   ; leading pad
       (push text chunks) (cl-incf len (length text))
       (when (or (< i (1- n))                  ; separator before the next insert, or
                 (and (= i (1- n)) trail))     ; trailing pad after the whole run
         (push " " chunks) (cl-incf len))
       (push (cons s (cons start len)) spans)))
    (save-excursion
      (goto-char marker)
      (insert (apply #'concat (nreverse chunks))))
    ;; MARKER (type nil) stayed at P, before the inserted text.
    (dolist (sp (nreverse spans))
      (notelinks--make-overlay (car sp) (+ p (cadr sp)) (+ p (cddr sp))))))

(defun notelinks--on-result (src env)
  (with-current-buffer src
    (let* ((raws (alist-get 'suggestions env))
           (all (mapcar #'notelinks--make-sug raws))
           ;; Drop low-confidence suggestions up front (engine returns them; we
           ;; only review those at or above `notelinks-min-confidence').
           (sugs (seq-filter (lambda (s) (>= (notelinks-sug-confidence s)
                                             notelinks-min-confidence))
                             all))
           (low (- (length all) (length sugs)))
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
        ;; Partition kept into wrap spans and insert groups.  Inserts sharing a
        ;; resolved position are grouped so they are laid out side by side
        ;; (space-separated) instead of stacking on top of each other.
        (let ((order (mapcar (lambda (r) (plist-get r :sug)) kept))
              wraps igroups)
          (dolist (r kept)
            (let ((s (plist-get r :sug)) (beg (plist-get r :beg)))
              (if (eq (notelinks-sug-mode s) 'insert)
                  (let ((cell (assoc beg igroups #'=)))
                    (if cell (setcdr cell (cons s (cdr cell)))
                      (push (cons beg (list s)) igroups)))
                (push r wraps))))
          (dolist (g igroups) (setcdr g (nreverse (cdr g))))  ; restore kept order
          ;; Phase 3a: pin every position with markers before any edit (so each
          ;; insertion's shift is absorbed by the others).
          (let ((wmarks (mapcar (lambda (r)
                                  (list (plist-get r :sug)
                                        (copy-marker (plist-get r :beg) nil)
                                        (copy-marker (plist-get r :end) nil)))
                                wraps))
                (gmarks (mapcar (lambda (g) (cons (copy-marker (car g) nil) (cdr g)))
                                igroups)))
            ;; Phase 3b: insert each group's prose and overlay its members.
            (dolist (gm gmarks)
              (notelinks--insert-group (car gm) (cdr gm))
              (set-marker (car gm) nil))
            ;; Phase 4: overlay the wrap spans from their final marker positions.
            (dolist (m wmarks)
              (notelinks--make-overlay (nth 0 m) (marker-position (nth 1 m))
                                       (marker-position (nth 2 m)))
              (set-marker (nth 1 m) nil) (set-marker (nth 2 m) nil)))
          (setq notelinks--suggestions order))
        ;; Report & enter review.
        (notelinks--report notelinks--suggestions unanchorable discarded low)
        (if (null notelinks--suggestions)
            (message "notelinks: no applicable suggestions")
          (notelinks-review-mode 1)
          (notelinks--show-panel)
          (notelinks--goto-first))))))

(defun notelinks--report (kept unanchorable discarded &optional low)
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
  (message "notelinks: %d shown%s%s%s"
           (length kept)
           (if unanchorable (format ", %d unanchorable" (length unanchorable)) "")
           (if discarded (format ", %d discarded" (length discarded)) "")
           (if (and low (> low 0))
               (format ", %d below confidence %d" low notelinks-min-confidence) "")))

;;;; Metainfo (side panel + help-echo)

(defun notelinks--truncate (s n)
  (if (and (stringp s) (> (length s) n)) (concat (substring s 0 n) "…") s))

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
              (format "\n  \"%s\"" (notelinks--truncate excerpt 200))))))

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

(defun notelinks--goto (s)
  (goto-char (overlay-start (notelinks-sug-overlay s)))
  (notelinks--update-panel))

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
  (notelinks--hide-panel)
  (when notelinks-review-mode (notelinks-review-mode -1))
  (message "notelinks: review done"))

;;;; Key legend (bottom side panel) + target info (posframe)

(defconst notelinks--panel-keys
  "  a accept   r reject   n next   p previous   j jump   q/C-g quit
  (off-overlay: C-c C-n / C-c C-p / C-c C-j / C-c C-q / C-g)"
  "Key legend shown in the bottom side panel for the whole session.")

(defvar notelinks-panel-mode-map
  (let ((m (make-sparse-keymap)))
    (define-key m "q" #'notelinks-panel-quit)
    (define-key m (kbd "C-g") #'notelinks-panel-quit)
    m)
  "Keymap for the key-legend side panel buffer.")

(define-derived-mode notelinks-panel-mode special-mode "NoteLinks-Panel"
  "Major mode for the notelinks key-legend side panel.")

(defun notelinks-panel-quit ()
  "Close the panel and quit the review session it belongs to."
  (interactive)
  (let ((src notelinks--panel-source))
    (if (buffer-live-p src)
        (progn (pop-to-buffer src)   ; leave point in the note, not the dying panel
               (notelinks-quit))
      (notelinks--hide-panel))))

;;; Bottom side panel — the static key legend.

(defun notelinks--render-panel ()
  "Populate the bottom panel buffer with the (static) key legend."
  (let ((src (current-buffer))
        (buf (get-buffer-create notelinks--panel-buffer-name)))
    (with-current-buffer buf
      (unless (derived-mode-p 'notelinks-panel-mode) (notelinks-panel-mode))
      (setq notelinks--panel-source src)
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert notelinks--panel-keys)
        (goto-char (point-min)))
      (setq-local mode-line-format nil))
    buf))

(defun notelinks--show-panel ()
  "Open the bottom side window with the key legend."
  (setq notelinks--panel-current :none)        ; force the first info render
  (setq notelinks--info-window
        (display-buffer-in-side-window
         (notelinks--render-panel)
         '((side . bottom) (window-height . 4))))
  (when (window-live-p notelinks--info-window)
    (fit-window-to-buffer notelinks--info-window 4 2)))

;;; Target info — a posframe near point (echo area on a TTY).

(defun notelinks--posframe-usable-p ()
  "Non-nil if posframe is available and can display on this frame."
  (and (require 'posframe nil t) (posframe-workable-p)))

(defun notelinks--screen-line-start (pos)
  "Return the start of the *screen* line containing POS.
Uses `vertical-motion' so it respects line wrapping (`visual-line-mode',
continuation lines) — `line-beginning-position' would jump to the logical
line start, which can be several screen rows above POS in a wrapped buffer."
  (save-excursion (goto-char pos) (vertical-motion 0) (point)))

(defun notelinks--show-info (s)
  "Show suggestion S's details in a posframe just below its span.
The posframe is anchored at the start of the **last screen line** of the
inserted/wrapped span and opens **downward**, so it sits below the whole span
(never covering it) and is left-aligned.  The anchor is the screen-line start
\(not the logical-line start) so it lands on the right row even when the span
wraps under `visual-line-mode'.  `posframe-poshandler-point-1' clamps the frame
within the parent frame — so it is never cut off at the right edge, and it
auto-flips above only when there is genuinely no room below.  A bare-point
fallback covers the (unexpected) overlay-less case."
  (if (notelinks--posframe-usable-p)
      (let* ((ov (notelinks-sug-overlay s))
             (anchor (notelinks--screen-line-start (if ov (overlay-end ov) (point)))))
        (posframe-show notelinks--info-buffer-name
                       :string (notelinks--describe s)
                       :position anchor
                       :poshandler #'posframe-poshandler-point-bottom-left-corner
                       :max-width 72
                       :internal-border-width 1
                       :internal-border-color "gray50"
                       :background-color (face-background 'tooltip nil t)))
    ;; No graphical frame (e.g. a TTY): fall back to the echo area.
    (message "%s" (notelinks--describe s))))

(defun notelinks--hide-info ()
  "Hide the target-info posframe, if one is shown."
  (when (and (featurep 'posframe) (get-buffer notelinks--info-buffer-name))
    (posframe-hide notelinks--info-buffer-name)))

(defun notelinks--update-panel ()
  "Track point: show the suggestion under point in the info posframe.
Bound to `post-command-hook' (buffer-local) and called after programmatic
moves; re-renders only when the suggestion under point changes, and hides
the posframe when point leaves every suggestion."
  (when notelinks-review-mode
    (let ((s (notelinks--at-point)))
      (unless (eq s notelinks--panel-current)
        (setq notelinks--panel-current s)
        (if s (notelinks--show-info s) (notelinks--hide-info))))))

(defun notelinks--hide-panel ()
  (when (and (featurep 'posframe) (get-buffer notelinks--info-buffer-name))
    (posframe-delete notelinks--info-buffer-name))
  (when (window-live-p notelinks--info-window)
    (delete-window notelinks--info-window))
  (setq notelinks--info-window nil
        notelinks--panel-current nil)
  (when-let ((buf (get-buffer notelinks--panel-buffer-name)))
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
    (define-key m (kbd "C-g") #'notelinks-quit)
    m)
  "Keymap active buffer-wide during a notelinks review session.")

(define-minor-mode notelinks-review-mode
  "Minor mode active while reviewing notelinks suggestions."
  :lighter " NoteLinks"
  :keymap notelinks-review-mode-map
  (if notelinks-review-mode
      (add-hook 'post-command-hook #'notelinks--update-panel nil t)
    (remove-hook 'post-command-hook #'notelinks--update-panel t)))

(provide 'notelinks)
;;; notelinks.el ends here
