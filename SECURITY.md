# Security Policy

## Supported Versions

This project is currently pre-1.0. Security fixes are applied to the latest release only.

## Reporting a Vulnerability

Please do not open public issues for token leaks or remote execution vulnerabilities.
Open a private security report with:

- environment (OS + Node + Python versions)
- exact command used
- redacted logs
- reproduction steps

## Secret Handling Rules

- Never commit Telegram bot tokens.
- Keep token files mode `600`.
- Do not print token values in logs.
- Run the bot with least privilege and avoid root services.
