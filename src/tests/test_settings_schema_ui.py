import pytest

from program.settings.models import AppModel, RTNSettingsModel
from program.settings.schema_ui import polish_settings_schema


def test_language_codes_normalised_and_checked():
    model = RTNSettingsModel(languages={"exclude": ["FR", " es", "fr"]})
    assert model.languages.exclude == ["fr", "es"]
    with pytest.raises(ValueError):
        RTNSettingsModel(languages={"exclude": ["xx"]})


def test_schema_titles_and_lists():
    schema = polish_settings_schema(AppModel.model_json_schema())
    defs = schema["$defs"]
    assert defs["UpdatersModel"]["properties"]["plex"]["title"] == "Plex"
    assert defs["QualityRankModel"]["properties"]["av1"]["title"] == "AV1"
    exclude = defs["LanguagesConfig"]["properties"]["exclude"]
    assert exclude["uniqueItems"] is True and "pattern" in exclude["items"]
    assert exclude["x-ui-options"] == {"orderable": False}
    assert exclude["items"]["x-ui-options"] == {"hideTitle": True}
    order = defs["ScraperModel"]["properties"]["anime_audio_order"]
    assert order["items"]["readOnly"] is True
    assert order["x-ui-options"] == {"addable": False, "removable": False}
