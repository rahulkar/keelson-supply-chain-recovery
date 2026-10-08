# Pinned third-party binaries for the Docker-free Windows mode (startup.bat).
# Versions follow compose.yaml where an upstream Windows build exists. Every archive is verified
# against its Sha256 (a mismatch deletes the file and stops); '' = not pinned (the hash is printed).
@{
    uv       = @{
        Version = '0.12.23'
        Url     = 'https://github.com/astral-sh/uv/releases/download/0.12.23/uv-x86_64-pc-windows-msvc.zip'
        Sha256  = '75d05de6762778c31ee183398de7dd15093fad0ed90b1f236d8205ea5ec00c90'
    }
    # Postgres 18 (compose: postgres:18 / postgis/postgis:18-3.6). EDB binaries zip, no installer.
    postgres = @{
        Version = '18.6-1'
        Url     = 'https://get.enterprisedb.com/postgresql/postgresql-18.6-1-windows-x64-binaries.zip'
        Sha256  = 'fbe23da234ee31547bf8a36d29dfd81e82b849df2d2b78d2eecb43d360252f8c'
    }
    # PostGIS 3.6 bundle for PG18 (OSGeo); unpacked over the Postgres tree.
    postgis  = @{
        Version = '3.6.2'
        Url     = 'https://download.osgeo.org/postgis/windows/pg18/postgis-bundle-pg18-3.6.2x64.zip'
        Sha256  = '0f41241cc536f7404dda43fd2a3f20ffe1fa1d71a8d4f6341428cd25931bf419'
    }
    # pgvector has no official Windows binaries: community build (github.com/andreiramani/pgvector_pgsql_windows).
    pgvector = @{
        Version = '0.8.6'
        Url     = 'https://github.com/andreiramani/pgvector_pgsql_windows/releases/download/0.8.6_18/vector.v0.8.6-pg18.zip'
        Sha256  = 'bda17eb97d9e687e3da701adbf4b65a342943b3e0cdc81935ccf0b9833a1ed62'
    }
    # Valkey has no Windows build; Redis for Windows speaks the same protocol (compose: valkey:8).
    redis    = @{
        Version = '8.10.2'
        Url     = 'https://github.com/redis-windows/redis-windows/releases/download/8.10.2/Redis-8.10.2-Windows-x64-msys2.zip'
        Sha256  = '7c8cebd50347eaa1d9e784da842ed47a4f33394637835531d6614777b950ee85'
    }
    keycloak = @{
        Version = '26.8.0'
        Url     = 'https://github.com/keycloak/keycloak/releases/download/26.8.0/keycloak-26.8.0.zip'
        Sha256  = '7ed1de3fda2598369262613bf682aab7e233d80a38c405e91588f7a7454370a1'
    }
    # compose: adorsys/keycloak-config-cli:6.5.1-26 (the newest Keycloak-26 jar of that release).
    kcconfig = @{
        Version = '6.5.1-26.5.5'
        Url     = 'https://github.com/adorsys/keycloak-config-cli/releases/download/v6.5.1/keycloak-config-cli-26.5.5.jar'
        Sha256  = 'df6939a1d9c40d5ab7e1fb690082ea8980dbbf402ca92a9968b90b652cb3ca9a'
    }
    caddy    = @{
        Version = '2.11.7'
        Url     = 'https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_windows_amd64.zip'
        Sha256  = '0a1edc0b799512051c57071ce0e798d3f2cf67dc98171366f1d2b326072e3b06'
    }
    # Installed with uv / npm (not downloaded as archives).
    litellm  = @{ Version = '1.104.0'; Python = '3.14' }
    pnpm     = @{ Version = '10.34.6' }
}
