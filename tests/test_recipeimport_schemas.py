from codex_farm.paths import find_repo_root
from codex_farm.schema_utils import validate_json_file_against_schema


def test_recipeimport_intermediate_examples_validate() -> None:
    repo_root = find_repo_root()
    schema_path = repo_root / "schemas" / "recipeimport_intermediate_fullshape_v1.schema.json"
    example_paths = [
        repo_root / "examples" / "recipeimport_intermediate" / "shaved-carrot-salad.r24.jsonld",
        repo_root / "examples" / "recipeimport_intermediate" / "platonic-full.example.jsonld",
    ]

    for example_path in example_paths:
        validate_json_file_against_schema(
            json_path=example_path,
            schema_path=schema_path,
        )


def test_recipeimport_final_examples_validate() -> None:
    repo_root = find_repo_root()
    schema_path = repo_root / "schemas" / "recipeimport_final_fullshape_v1.schema.json"
    example_paths = [
        repo_root / "examples" / "recipeimport_final" / "shaved-carrot-salad.r24.json",
        repo_root / "examples" / "recipeimport_final" / "platonic-full.example.json",
    ]

    for example_path in example_paths:
        validate_json_file_against_schema(
            json_path=example_path,
            schema_path=schema_path,
        )


def test_recipeimport_benchmark_line_label_example_validates() -> None:
    repo_root = find_repo_root()
    schema_path = repo_root / "schemas" / "recipeimport_benchmark_line_label_v1.schema.json"
    example_path = repo_root / "examples" / "recipeimport_benchmark" / "line_label_predictions.example.json"
    validate_json_file_against_schema(
        json_path=example_path,
        schema_path=schema_path,
    )
