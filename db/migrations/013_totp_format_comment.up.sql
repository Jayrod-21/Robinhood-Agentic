-- 013_totp_format_comment — correct the stored ciphertext layout recorded in the catalog.
--
-- WHY
--   012's comment on operator_totp says the stored value is base64(nonce ‖ tag ‖ ciphertext).
--   The single implementation — backend/app/services/crypto.py, which both the backend and
--   bin/manage_operator.py import — produces base64(nonce ‖ ciphertext ‖ tag), because
--   cryptography's AESGCM.encrypt() returns ciphertext with the 16-byte tag APPENDED, and the
--   module stores nonce + that.
--
--   Nothing is broken today: one implementation writes and reads the value, so it round-trips, and
--   012's CHECK constraints only assert base64-ness and a length floor. The defect is that the
--   catalog states a byte layout that is not the one on disk. Anyone writing a second reader — a
--   migration to a KMS, an offline recovery script, a rewrite in another language — would follow
--   the comment, slice the wrong 16 bytes, and get an authentication failure that looks like a
--   wrong key rather than a wrong offset.
--
--   That is this project's recurring defect exactly: a stored value whose description means
--   something other than what it says. It is cheap to fix here and expensive to fix later.
--
-- WHY A NEW MIGRATION
--   012 is applied and checksum-locked; editing it raises ChecksumMismatch by design. Corrections
--   go in a new migration as COMMENT ON — the same pattern 010 used to fix 001's untrue claim
--   about rh_app authentication.
--
--   NOTE: the `--` prose inside 012's own file still carries the wrong order and cannot be edited
--   for the same reason. crypto.py's module docstring is the authoritative statement of the format
--   and now says so explicitly.
--
-- migrate: filename carries no destructive marker — this migration only rewrites comments.

COMMENT ON TABLE operator_totp IS
    'TOTP enrolment + §5.8 lockout state. secret_encrypted is AES-256-GCM ciphertext only '
    '(base64(nonce ‖ ciphertext ‖ tag) — 12-byte nonce, then AESGCM.encrypt() output, which is the '
    'ciphertext with its 16-byte tag appended); the key is TOTP_SECRET_ENC_KEY in backend/.env and '
    'must never enter this database (§7). Login reads only rows with confirmed_at IS NOT NULL. '
    'Authoritative implementation: backend/app/services/crypto.py — 012''s comment stated the tag '
    'before the ciphertext, which was wrong; corrected by 013.';

COMMENT ON COLUMN operator_totp.secret_encrypted IS
    'Ciphertext, never plaintext and never the key. Layout is base64(nonce(12) ‖ ciphertext ‖ '
    'tag(16)) — see backend/app/services/crypto.py, the one implementation both the backend and '
    'bin/manage_operator.py import. Length floor 40 means a bare base32 secret cannot be stored '
    'here by accident.';
