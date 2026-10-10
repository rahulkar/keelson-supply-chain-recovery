# Vendored upstream signing keys

Public keys of third-party publishers whose images Keelson verifies before use (docs/supply-chain.md).
A key changes only through a reviewed pull request (`.github/CODEOWNERS`); after an upstream key
rotation, verification fails closed in CI (`release.yml`) and in `scripts/supply-chain/verify-images.sh
--compose` until the new key is vendored here.

| File | Publisher | Verifies | Source | Fetched | SHA-256 of the file |
|---|---|---|---|---|---|
| `litellm-cosign.pub` | BerriAI (ECDSA P-256, cosign key pair) | `ghcr.io/berriai/litellm-non_root` (compose `litellm`, `LITELLM_IMAGE` in `infra/cloudrun/versions.env`, base of `infra/cloudrun/images/litellm.Dockerfile`) | `https://github.com/BerriAI/litellm/blob/0112e53046018d726492c814b3644b7d376029d0/cosign.pub` (added in that commit; identical on `main` at `bbc8d663234d7b464c65dfb93547de3d0b680560`) | 2026-10-10 | `ff8869bf14ba9d10af7b64b9d479543b44daec0165e715753c43ff8a998f6dd3` |

Check (needs the network; Rekor inclusion is verified too):

```bash
cosign verify --key infra/supply-chain/keys/litellm-cosign.pub \
  ghcr.io/berriai/litellm-non_root:1.104.0@sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e
# or, without a local cosign: scripts/supply-chain/verify-images.sh --compose
```

Images of the other third-party services (Keycloak, keycloak-config-cli, Postgres, PostGIS, Valkey,
Caddy, Squid, Phoenix) carry no publisher signature that Keelson relies on: they are pinned by digest
only.
