"""TMDB API client"""

from program.utils.request import SmartSession

TMDB_READ_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJlNTkxMmVmOWFhM2IxNzg2Zjk3ZTE1NWY1YmQ3ZjY1MSIsInN1YiI6IjY1M2NjNWUyZTg5NGE2MDBmZjE2N2FmYyIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.xrIXsMFJpI1o1j5g2QpQcFP1X3AfRjFA5FlBFO5Naw8"  # noqa: S105


class TMDBApiError(Exception):
    """Base exception for TMDB API related errors"""


class TMDBApi:
    """Handles TMDB API communication"""

    def __init__(self):
        self.BASE_URL = "https://api.themoviedb.org/3"

        self.session = SmartSession(
            base_url=self.BASE_URL,
            rate_limits={
                # 40 requests per second
                # https://developer.themoviedb.org/docs/rate-limiting
                "api.themoviedb.org": {
                    "rate": 40,
                    "capacity": 1000,
                }
            },
            retries=2,
            backoff_factor=0.3,
        )

        self.session.headers.update(
            {
                "Authorization": f"Bearer {TMDB_READ_ACCESS_TOKEN}",
            }
        )

    def get_from_external_id(self, external_source: str, external_id: str):
        """Get TMDB item from external ID"""

        response = self.session.get(
            f"find/{external_id}?external_source={external_source}"
        )

        from schemas.tmdb import FindById200Response

        return FindById200Response.from_dict(response.json())

    def get_movie_details_with_external_ids_and_release_dates(self, movie_id: str):
        """Get movie details with external IDs and release dates appended"""

        response = self.session.get(
            f"movie/{movie_id}?append_to_response=external_ids,release_dates"
        )

        from schemas.tmdb import (
            MovieDetails200Response,
            MovieExternalIds200Response,
            MovieReleaseDates200Response,
        )

        class MovieDetailsWithExtras(MovieDetails200Response):
            external_ids: MovieExternalIds200Response
            release_dates: MovieReleaseDates200Response

        data = response.json()

        movie_details = MovieDetails200Response.from_dict(data)
        external_ids = MovieExternalIds200Response.from_dict(data.get("external_ids"))
        release_dates = MovieReleaseDates200Response.from_dict(
            data.get("release_dates")
        )

        assert movie_details
        assert external_ids
        assert release_dates

        return MovieDetailsWithExtras.model_validate(
            {
                **movie_details.model_dump(),
                "external_ids": external_ids,
                "release_dates": release_dates,
            }
        )

    def search(self, kind: str, query: str, year: int | None = None) -> list[dict]:
        """Search TMDB movies (`kind="movie"`) or shows (`kind="tv"`) by title."""

        params: dict[str, str | int] = {"query": query, "include_adult": "false"}

        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year

        response = self.session.get(f"search/{kind}", params=params)

        if not response.ok:
            raise TMDBApiError(f"TMDB search failed with HTTP {response.status_code}")

        return response.json().get("results") or []

    def get_tvdb_id(self, tmdb_tv_id: int | str) -> str | None:
        """Return the TVDB id TMDB lists for a show, if any."""

        response = self.session.get(f"tv/{tmdb_tv_id}/external_ids")

        if not response.ok:
            raise TMDBApiError(
                f"TMDB external ids failed with HTTP {response.status_code}"
            )

        tvdb_id = response.json().get("tvdb_id")

        return str(tvdb_id) if tvdb_id else None
