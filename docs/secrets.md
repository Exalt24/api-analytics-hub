# Where the secrets live

The rule is one place per secret, none of them in git, and a test that fails when one gets in.

| Secret | Where it lives | Who can read it |
|---|---|---|
| Provider token for Terraform (`DIGITALOCEAN_TOKEN`) | the shell that runs `terraform`, exported for that session | the person running it |
| The backup units' settings (`DATABASE_URL`, `SLACK_WEBHOOK_URL`) | `/etc/hub/hub.env` on the server, created empty by cloud-init with mode `0600`, owner `root`; the two backup units name it in `EnvironmentFile=` | root, and those units |
| Third-party credentials a tenant connects (Shopify and the other connectors) | the database in `platform_connection.credentials_enc`, sealed with AES-256-GCM by `app/crypto.py` under the keys in `CREDENTIAL_KEYS` | the app only; the plaintext sits in a wrapper that refuses to print or format |
| `CREDENTIAL_KEYS` itself | an environment variable of the process that runs the app, written as `1:<base64>,2:<base64>` | whoever starts that process |
| Local development | `.env` and `.secrets/`, both ignored by git | the developer |

## Putting a value on the server

```bash
ssh root@<droplet_ipv4>
umask 077
nano /etc/hub/hub.env        # KEY=value lines; the file is root-only already
```

Nothing is typed into a command line (it lands in shell history and the process list) and nothing is passed to Terraform: `terraform.tfvars` and the state file are ignored by git because they would carry every value Terraform read. The backup timers read `hub.env` at each run, so a changed value is picked up the next time one fires.

## Rotating the credential key

`app/crypto.py` already supports it: each sealed value starts with the version of the key that sealed it, the highest version in `CREDENTIAL_KEYS` encrypts, and every configured version can still decrypt. To rotate, add a new highest version, restart the app, and keep the old version listed until no row still carries it. The code stops short of one thing: there is no command yet that re-seals the old rows under the new key, so the old key stays configured until that is written.

Each sealed value is also bound to its own row (tenant, connection and platform go in as associated data), so a blob copied from one tenant's row into another's fails to decrypt instead of working.

## What stops a secret reaching git

- `.gitignore` covers `.env`, `.secrets/`, `*.token`, `*.tfstate` and `*.tfvars`.
- `tests/test_no_secrets_in_repo.py` scans every file git would commit for DigitalOcean, Stripe, Shopify, Slack, GitHub and AWS tokens, private key blocks and database URLs with a password on a real host. The documented throwaway database on `127.0.0.1` passes by rule. A control per pattern shows it can fail, and a planted token file in a scratch run turned the scan red.
- cloud-init is checked for secrets by `tests/test_deploy_digitalocean.py`.

## Limits

This is files and permissions, not a secrets manager: no audit trail of who read a value, no automatic rotation, no re-seal command, and anyone with root on the droplet can read `/etc/hub/hub.env`. A KMS or a managed secrets store is the next step.
