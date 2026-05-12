from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class VoiceSessionMigrateValue(BaseModel):
    reason: Literal["sarvam_region_preferred"] = "sarvam_region_preferred"
    target_region: Literal["bom", "sin"]
    new_room_url: str
    new_client_token: str
    valid_for_seconds: int = Field(default=120, ge=1)
    preserve_session_id: bool = True


class VoiceSessionMigrate(BaseModel):
    type: Literal["voice_session_migrate"] = "voice_session_migrate"
    value: VoiceSessionMigrateValue
