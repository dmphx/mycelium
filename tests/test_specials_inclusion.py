"""Explicit story-special selections, using synthetic catalogue identities."""
import copy

import pytest

from specials_inclusion import normalize_policy, select_catalog


def policy_rows():
    return [
        {"episode": 2, "tmdb_episode_id": 10002, "title": "First Special", "air_date": "2024-12-25"},
        {"episode": 545, "tmdb_episode_id": 10545, "title": "Second Special", "air_date": "2025-12-25"},
    ]


def catalog():
    selected = [
        {"episode_number": r["episode"], "id": r["tmdb_episode_id"], "name": r["title"],
         "air_date": r["air_date"], "season_number": 0, "show_id": 9000}
        for r in policy_rows()
    ]
    return [{"episode_number": 1, "id": 10001, "name": "An Extra", "air_date": "2024-01-01",
             "season_number": 0, "show_id": 9000}] + selected


def test_only_explicit_story_specials_selected_not_all_season_zero():
    policy = normalize_policy("tt1234567", 9000, policy_rows())
    original = catalog()
    before = copy.deepcopy(original)
    assert [r["episode_number"] for r in select_catalog(policy, "tt1234567", 9000, original)] == [2, 545]
    assert original == before


@pytest.mark.parametrize("field,value", [("imdb_id", "tt7654321"), ("tmdb_id", 9001)])
def test_other_era_identity_refused(field, value):
    policy = normalize_policy("tt1234567", 9000, policy_rows())
    args = {"imdb_id": "tt1234567", "tmdb_id": 9000}
    args[field] = value
    with pytest.raises(ValueError, match="show identity"):
        select_catalog(policy, **args, episodes=catalog())


@pytest.mark.parametrize("field,value", [("id", 999), ("name", "A New Edit"),
                                         ("air_date", "2025-01-01"), ("show_id", 9001),
                                         ("season_number", 1)])
def test_selected_episode_metadata_drift_refuses(field, value):
    policy = normalize_policy("tt1234567", 9000, policy_rows())
    episodes = catalog()
    episodes[1][field] = value
    with pytest.raises(ValueError, match="selected special"):
        select_catalog(policy, "tt1234567", 9000, episodes)


def test_missing_selected_episode_refuses_instead_of_silently_dropping():
    policy = normalize_policy("tt1234567", 9000, policy_rows())
    with pytest.raises(ValueError, match="missing selected special"):
        select_catalog(policy, "tt1234567", 9000, catalog()[:-1])


def test_duplicate_catalog_number_refuses_ambiguous_selected_identity():
    policy = normalize_policy("tt1234567", 9000, policy_rows())
    episodes = catalog()
    episodes.append(dict(episodes[1], id=20002))
    with pytest.raises(ValueError, match="ambiguous selected special"):
        select_catalog(policy, "tt1234567", 9000, episodes)


@pytest.mark.parametrize("bad", [True, 0, -1, "2", 10000])
def test_invalid_selection_number_refuses(bad):
    rows = policy_rows()
    rows[0]["episode"] = bad
    with pytest.raises(ValueError):
        normalize_policy("tt1234567", 9000, rows)


def test_duplicate_selection_number_or_native_id_refuses():
    for key in ("episode", "tmdb_episode_id"):
        rows = policy_rows()
        rows[1][key] = rows[0][key]
        with pytest.raises(ValueError, match="duplicate selected"):
            normalize_policy("tt1234567", 9000, rows)


def test_empty_selection_never_means_all_specials():
    with pytest.raises(ValueError, match="explicit"):
        normalize_policy("tt1234567", 9000, [])


@pytest.mark.parametrize("bad", ["2024-02-30", "2024-1-1", None])
def test_unknown_or_noncanonical_air_date_requires_review(bad):
    rows = policy_rows()
    rows[0]["air_date"] = bad
    with pytest.raises(ValueError):
        normalize_policy("tt1234567", 9000, rows)


def test_identity_roundtrip_normalizes_only_unicode_and_whitespace():
    rows = policy_rows()
    rows[0]["title"] = " First   Special "
    policy = normalize_policy("tt1234567", 9000, rows)
    assert policy["episodes"][0]["title"] == "First Special"
    assert select_catalog(policy, "tt1234567", 9000, catalog())


@pytest.mark.parametrize("numbers,total", [
    ([41, 545], 1384),
    ([2, 4, 7, 9, 13, 14, 16, 17, 34, 47, 66, 83, 84, 142, 148, 149,
      154, 156, 158, 208, 209, 210], 199),
    ([1, 2, 3, 4, 5], 5),
])
def test_story_selection_counts_never_expand_to_catalogue_size(numbers, total):
    # Synthetic native IDs keep the era-specific numbering independent.
    rows = [{"episode": n, "tmdb_episode_id": 10000 + n,
             "title": f"Special {n}", "air_date": "2024-12-25"} for n in numbers]
    extras = [n for n in range(1, total + len(numbers) + 1) if n not in numbers]
    catalogue_numbers = sorted(numbers + extras[:total - len(numbers)])
    current = [{"episode_number": n, "id": 10000 + n, "name": f"Special {n}",
                "season_number": 0, "air_date": "2024-12-25"}
               for n in catalogue_numbers]
    assert len(current) == total
    policy = normalize_policy("tt1234567", 9000, rows)
    assert [ep["episode_number"] for ep in select_catalog(policy, "tt1234567", 9000, current)] == numbers
