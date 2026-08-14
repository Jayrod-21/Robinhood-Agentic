-- Down for 013 — restore 012's comment text verbatim.
--
-- Non-destructive in both directions: this only rewrites catalog comments, touches no row and no
-- privilege, so the filename carries no destructive marker (ADR-002).
--
-- Restoring means restoring the WRONG byte order, because that is what 012 said. That is correct
-- behaviour for a down migration — it returns the schema to the state 012 left it in, defects
-- included. It does not mean the order is right.

COMMENT ON TABLE operator_totp IS
    'TOTP enrolment + §5.8 lockout state. secret_encrypted is AES-256-GCM ciphertext only '
    '(base64(nonce‖tag‖ciphertext)); the key is TOTP_SECRET_ENC_KEY in backend/.env and must never '
    'enter this database (§7). Login reads only rows with confirmed_at IS NOT NULL.';

COMMENT ON COLUMN operator_totp.secret_encrypted IS
    'Ciphertext, never plaintext and never the key. Length floor 40 means a bare base32 secret '
    'cannot be stored here by accident.';
