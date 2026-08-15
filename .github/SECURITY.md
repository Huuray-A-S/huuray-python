# Security policy

## Reporting a vulnerability

Email **rg@huuray.com** with the details. Please do not open a public issue, and do not post about it publicly before we have responded.

Useful to include: what you found, how to reproduce it, the impact you think it has, and how you would like to be credited.

You can expect an acknowledgement within five business days.

## Scope

This policy covers **this client library**. Vulnerabilities in the Huuray API, the Huuray platform, or huuray.com should go to the same address and will be routed internally.

## Supported versions

The most recent minor release receives security fixes.

## Please never include in a report, an issue, or a pull request

- API tokens or API secrets
- Voucher codes, CVVs, or redeem links — these are **bearer instruments**: whoever holds the code holds the value
- Real recipient names, email addresses, or phone numbers

If you need to demonstrate something with real data, say so in your email and we will arrange a private channel.

## Notes for contributors

- Test fixtures must contain only invented data. Never record a real API response into the suite.
- The library redacts voucher codes, credentials and contact details from anything it prints, and `Voucher.__repr__` masks the code, CVV and redeem link. If you add a field that carries value or personal data, add it to `SECRET_FIELDS` or `SENSITIVE_FIELDS` in [`src/huuray/redact.py`](../src/huuray/redact.py) and cover it with a test.
- Never log a request body from the order endpoints without passing it through `redact()` first.
