from __future__ import annotations

from voice.protocol.migration import VoiceSessionMigrate, VoiceSessionMigrateValue


def test_voice_session_migrate_round_trips_json() -> None:
    frame = VoiceSessionMigrate(
        value=VoiceSessionMigrateValue(
            target_region="bom",
            new_room_url="https://orchet.daily.co/room-bom",
            new_client_token="client-token",
        )
    )

    encoded = frame.model_dump_json()
    decoded = VoiceSessionMigrate.model_validate_json(encoded)

    assert decoded.type == "voice_session_migrate"
    assert decoded.value.reason == "sarvam_region_preferred"
    assert decoded.value.target_region == "bom"
    assert decoded.value.valid_for_seconds == 120
    assert decoded.value.preserve_session_id is True
