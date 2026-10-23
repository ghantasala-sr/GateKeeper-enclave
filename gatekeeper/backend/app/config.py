import os
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repo root so docker compose and the app share it.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    # SECRETS_DIR: a directory of files, one per setting (e.g. session_secret), as
    # mounted from Secret Manager by GKE's CSI driver. Environment variables win.
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore",
                                      secrets_dir=os.environ.get("SECRETS_DIR") or None)

    # Owner: migrations, seed, ingest ONLY. Leave unset for the running API, which
    # must never hold the owner's password; scripts fail clearly without it.
    migration_database_url: str = ""
    app_database_url: str = ""   # gatekeeper_app: the running app
    app_db_password: str = ""
    # Or give the parts, and the URLs are built here (so no file holds a whole URL):
    database_host: str = ""
    database_port: int = 5432
    database_name: str = "gatekeeper"
    owner_user: str = "gatekeeper_owner"
    owner_password: str = ""

    # Chat model. The Enclave uses vertex-gemini only; anthropic is for local development.
    llm_provider: Literal["vertex-gemini", "anthropic"] = "anthropic"  # checked at startup
    anthropic_api_key: str = ""
    chat_model: str = "claude-opus-5-5"          # Anthropic model id
    vertex_project: str = ""
    vertex_location: str = "global"
    gemini_model: str = "gemini-3.8-flash"
    vertex_timeout_seconds: int = 20     # per attempt
    vertex_retry_attempts: int = 4       # 429/503 from the shared Gemini quota, with backoff

    okta_issuer: str = ""
    okta_client_id: str = ""
    okta_client_secret: str = ""
    okta_redirect_uri: str = "http://localhost:8000/auth/okta/callback"

    # Globex SAML. The IdP entity ID is pinned here, not trusted from metadata.
    saml_idp_metadata_url: str = ""
    saml_idp_entity_id: str = ""
    saml_sp_entity_id: str = "urn:gatekeeper:globex"
    saml_sp_acs_url: str = "http://localhost:8000/auth/saml/acs"

    session_secret: str

    frontend_url: str = "http://localhost:3000"

    token_signing_key_b64: str
    # Previous signing keys (comma-separated base64 PEMs) still accepted during a rotation.
    token_verify_keys_b64: str = ""
    token_issuer: str = "http://localhost:8000"
    token_audience: str = "gatekeeper-api"
    token_ttl_seconds: int = 900  # 15 minutes

    @model_validator(mode="after")
    def _database_urls(self):
        def url(user, password):
            return f"postgresql://{user}:{quote(password, safe='')}@{self.database_host}:{self.database_port}/{self.database_name}"
        if not self.app_database_url and self.database_host and self.app_db_password:
            self.app_database_url = url("gatekeeper_app", self.app_db_password)
        if not self.migration_database_url and self.database_host and self.owner_password:
            self.migration_database_url = url(self.owner_user, self.owner_password)
        if not self.app_database_url:
            raise ValueError("set APP_DATABASE_URL, or DATABASE_HOST and APP_DB_PASSWORD")
        return self

    app_version: str = "dev"  # the release, set by the chart
    metrics_port: int = 0  # 0 = off; the chart sets 9090 (never routed by the Gateway)


settings = Settings()
