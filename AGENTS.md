# Backend project database and safety rules

- This project currently uses SQLite. It may move to PostgreSQL in the future and may evaluate Supabase, but do not start that migration now.
- Keep the current SQLite database and application behavior working. Prefer portable SQL where it does not reduce correctness.
- Perform every schema change through the project's migration framework. Review a rollback or recovery plan before changing schema.
- Require separate user confirmation for destructive SQL and for any production data modification.
- Never put secrets, credentials, tokens, or connection strings in source code or Git.
- Enable and verify SQLite foreign-key enforcement wherever database connections are created or tested.
- Account for SQLite and PostgreSQL differences in types, defaults, constraints, indexes, transactions, locking, case sensitivity, and SQL syntax.
- Run existing relevant tests and linters after changes. Do not claim cross-database compatibility without testing both engines.
- During security review, verify data flow, reachability, existing mitigations, exploit preconditions, and practical impact before reporting a vulnerability.
