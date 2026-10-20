from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repo root so docker compose and the app share it.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    migration_database_url: str  # owner: migrations, seed, ingest ONLY
    app_database_url: str        # gatekeeper_app: the running app
    app_db_password: str = ""
    anthropic_api_key: str = ""
    chat_model: str = "claude-opus-5-5"

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
    token_issuer: str = "http://localhost:8000"
    token_audience: str = "gatekeeper-api"
    token_ttl_seconds: int = 900  # 15 minutes


settings = Settings()
