import identity_repair


def test_path_identity_parses_zero_padded_episode_name():
    season, episode = identity_repair._path_identity(
        "/data/media/series/Example/Season 02/Example.S02E013.strm")
    assert (season, episode) == (2, 13)


def test_path_identity_uses_season_folder_and_episode_only_name():
    season, episode = identity_repair._path_identity(
        "/data/media/series/Example/Season 7/Example Episode 4.strm")
    assert (season, episode) == (7, 4)


def test_title_normalization_keeps_exact_identity_matching_conservative():
    assert identity_repair._norm_title("The Example (2024)") == "theexample"
    assert identity_repair._norm_title("Example: Redux") != "theexample"
