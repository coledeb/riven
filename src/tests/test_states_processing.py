import pytest

from program.media.item import Movie
from program.media.state import States


@pytest.fixture
def movie():
    return Movie({"imdb_id": "tt1375666", "requested_by": "Iceberg"})


def test_requested_state(movie):
    """Test transition to the Requested state."""
    # Given: A media item (movie)
    movie.set("requested_by", "user")
    # When: The item is requested by a user
    # Then: The item's state should be Requested
    assert movie.state == States.Requested, "Movie should transition to Requested state"
