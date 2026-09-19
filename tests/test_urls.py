from __future__ import annotations

from pydantic import HttpUrl

from jenkins_stats.models import joined_url


def test_append_url_adds_path_suffix_without_duplicate_separator() -> None:
    # Given equivalent Jenkins item URLs with and without a trailing slash.
    slash_terminated = HttpUrl("https://jenkins.example/job/example/")
    bare_path = HttpUrl("https://jenkins.example/job/example")

    # When a Jenkins API suffix is appended with or without a leading slash.
    appended_from_slash = joined_url(slash_terminated, "/api/json")
    appended_from_bare = joined_url(bare_path, "api/json")

    # Then both forms produce the same single-separator endpoint URL.
    assert appended_from_slash == HttpUrl(
        "https://jenkins.example/job/example/api/json"
    )
    assert appended_from_bare == HttpUrl("https://jenkins.example/job/example/api/json")


def test_append_url_preserves_context_and_encoded_paths() -> None:
    # Given a Jenkins URL under a context path with an encoded branch name.
    branch_url = HttpUrl(
        "https://jenkins.example/jenkins/job/repo/job/feature%2Fencoded/"
    )

    # When a Pipeline timing suffix is appended.
    endpoint = joined_url(branch_url, "wfapi/describe")

    # Then the context path and encoded path segment are preserved.
    assert endpoint == HttpUrl(
        "https://jenkins.example/jenkins/job/repo/job/feature%2Fencoded/wfapi/describe"
    )


def test_append_url_keeps_suffix_root_relative_to_input_url_path() -> None:
    # Given a controller URL at the origin root.
    base_url = HttpUrl("https://jenkins.example/")

    # When a suffix with extra surrounding slashes is appended.
    endpoint = joined_url(base_url, "/api/json/")

    # Then the suffix is appended to that URL path, not joined as an absolute URL.
    assert endpoint == HttpUrl("https://jenkins.example/api/json/")
