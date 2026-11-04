# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Process settings. Holds no credentials: those arrive per request as a header."""

from pydantic_settings import BaseSettings, SettingsConfigDict

API_BASE_URL = "https://api.notion.com/v1"

# Notion ships dated API versions. Pinned here as a named constant rather than
# guessed in the design (PRD-21313 design, "Risks"): re-read
# https://developers.notion.com/reference/versioning at a later date if this
# needs to move. 2022-06-28 is also the last version before Notion's 2025
# multi-data-source database model, which v1 of this service intentionally
# does not support (PRD-21313 design, "Open questions" / "Non-goals") -- a
# newer pinned version would start returning the multi-source database shape
# this server does not branch on.
NOTION_VERSION = "2022-06-28"

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class Settings(BaseSettings):
    # extra="ignore": the runtime injects variables this project does not declare.
    model_config = SettingsConfigDict(extra="ignore")

    mcp_http_port: int = 8080
    mcp_http_host: str = "0.0.0.0"
    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings()
