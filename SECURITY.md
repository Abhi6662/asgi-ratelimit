# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in asgi-ratelimit, please report it by:

1. **Do not** open a public issue
2. Email the maintainer or use GitHub's private vulnerability reporting feature
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We will respond within 48 hours and work with you to address the issue promptly.

## Security Considerations

- Rate limiting is defense-in-depth, not a substitute for proper authentication
- Configure limits appropriate to your application's threat model
- Monitor for distributed attacks that may bypass per-IP limits
- Use HTTPS to prevent header manipulation attacks
