from matterhorn.contracts import SchemaProfile
from matterhorn.contracts.schema import resolve_schema
from matterhorn.engine.engine import Engine


def test_engine_accepts_builtin_id_path_and_profile_instance(tmp_path) -> None:
    builtin = resolve_schema("org-matters/v1")
    resource = resolve_schema("personal-decisions/v1")
    assert builtin.schema_id == "org-matters/v1"
    assert resource.schema_id == "personal-decisions/v1"

    resource_path = files("matterhorn.schemas").joinpath("org-matters/v1.yaml")
    with as_file(resource_path) as package_path:
        forms = ["org-matters/v1", package_path, builtin]
        for index, form in enumerate(forms):
            engine = Engine(tmp_path / f"form-{index}.db", form)
            assert isinstance(engine.profile, SchemaProfile)
            assert engine.profile.schema_id == "org-matters/v1"
from importlib.resources import as_file, files
