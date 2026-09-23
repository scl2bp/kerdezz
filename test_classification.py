from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from classification import analyze_source_features, fuse_classification


def _source(path: Path, source_id: str) -> dict[str, str]:
    return {
        "source_id": source_id,
        "member_name": path.name,
        "path": path.name,
        "file_sha256": f"{source_id}-hash",
    }


def test_regular_grid_preserves_arbitrary_row_and_column_counts(tmp_path: Path) -> None:
    image = Image.new("RGB", (360, 240), "white")
    draw = ImageDraw.Draw(image)
    for x in (120, 240):
        draw.line((x, 0, x, 240), fill="black", width=4)
    for y in (80, 160):
        draw.line((0, y, 360, y), fill="black", width=4)
    path = tmp_path / "grid.jpg"
    image.save(path)

    features = analyze_source_features(_source(path, "grid"), tmp_path)

    assert features["layout_observation"]["family"] == "grid"
    assert features["layout_observation"]["row_count"] == 3
    assert features["layout_observation"]["column_count"] == 3
    assert len(features["region_candidates"]) == 9


def test_composite_layout_preserves_dominant_grid_and_horizontal_pair(tmp_path: Path) -> None:
    image = Image.new("RGB", (300, 520), "white")
    draw = ImageDraw.Draw(image)
    for top, bottom in ((0, 170), (170, 340)):
        for left, right in ((0, 100), (100, 200), (200, 300)):
            draw.rounded_rectangle((left + 2, top + 2, right - 2, bottom - 2), radius=8, outline="black", width=4)
    for left, right in ((0, 150), (150, 300)):
        draw.rounded_rectangle((left + 2, 395, right - 2, 518), radius=8, outline="black", width=4)
    path = tmp_path / "composite.jpg"
    image.save(path)

    features = analyze_source_features(_source(path, "composite"), tmp_path)

    layout = features["layout_observation"]
    assert layout["family"] == "composite_regions"
    assert layout["composite"]["dominant_layout"]["row_count"] == 2
    assert layout["composite"]["dominant_layout"]["column_count"] == 3
    assert layout["composite"]["secondary_layout"]["orientation"] == "horizontal"
    assert len(features["region_candidates"]) == 8
    assert {region["group_id"] for region in features["region_candidates"]} == {
        "dominant_vertical_grid",
        "secondary_horizontal_pair",
    }
    assert {region["orientation"] for region in features["region_candidates"]} == {"vertical", "horizontal"}


def test_saturated_collection_outlier_is_non_card_without_accepted_regions() -> None:
    source = {"source_id": "board", "file_sha256": "board-hash"}
    features = {
        "region_candidates": [{"region_id": "candidate"}],
        "layout_observation": {"family": "composite_regions"},
        "image_statistics": {"color": {"saturation_mean": 0.54, "saturated_fraction": 0.76}},
        "feature_summary": {"possible_non_card_signal": 0.2},
    }
    evaluation = {
        "deterministic_context": {
            "grid_source_count": 3,
            "color_statistics": {
                "saturation_mean": {"mean": 0.03, "stddev": 0.01},
                "saturated_fraction": {"mean": 0.003, "stddev": 0.002},
            },
        },
        "feature_records": [],
        "model": {"response": None},
    }

    decision = fuse_classification(source, features, evaluation)

    assert decision["routing_class"] == "non_card"
    assert decision["accepted_region_count"] == 0
    assert "color_distribution_outlier" in decision["observation_profile"]["anomalies"]