from datetime import datetime
from types import SimpleNamespace

from program.utils import notify
from program.utils.notify import RED, Notice, item_title


def show_episode():
    show = SimpleNamespace(
        type="show",
        title="Show",
        log_string="Show",
        aired_at=datetime(2019, 5, 1),
        tmdb_id="42",
        poster_path="https://img/show.jpg",
        parent=None,
    )
    season = SimpleNamespace(type="season", log_string="Show S01", parent=show)
    return SimpleNamespace(
        type="episode",
        log_string="Show S01E02",
        poster_path="https://img/still.jpg",
        parent=season,
    )


def test_episode_uses_show_title_poster_and_link():
    episode = show_episode()
    notice = Notice(event="E", title=item_title(episode)).with_item(
        episode, "https://request.example/"
    )

    assert notice.title == "Show (2019) S01E02"
    assert notice.image == "https://img/show.jpg"
    assert notice.url == "https://request.example/tv/42"


def test_link_defaults_to_tmdb():
    notice = Notice(event="E", title="t").with_item(show_episode())

    assert notice.url == "https://www.themoviedb.org/tv/42"


def test_show_without_tmdb_id_is_looked_up_by_tvdb(monkeypatch):
    episode = show_episode()
    show = episode.parent.parent
    show.tmdb_id, show.tvdb_id = None, "76568"
    monkeypatch.setattr(notify, "_tmdb_for_tvdb", lambda i: {"76568": "4586"}.get(i))

    assert Notice(event="E", title="t").with_item(episode, "https://s").url == "https://s/tv/4586"

    show.tvdb_id = "1"
    assert Notice(event="E", title="t").with_item(episode).url == (
        "https://thetvdb.com/dereferrer/series/1"
    )


def test_discord_gets_embed_and_others_get_apprise(monkeypatch):
    posted, apprised = [], []

    def post(url, json, timeout):
        posted.append((url, json))
        return SimpleNamespace(raise_for_status=lambda: None)

    class FakeApprise:
        def __init__(self):
            self.urls = []

        def add(self, url):
            self.urls.append(url)

        def __len__(self):
            return len(self.urls)

        def notify(self, title, body):
            apprised.append((self.urls, title, body))

    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setattr(notify, "Apprise", FakeApprise)

    notify.send(
        [
            "https://discord.com/api/webhooks/1/abc",
            "discord://2/def",
            "ntfy://topic",
        ],
        Notice(
            event="Uncached Download Failed",
            title="Movie (2020)",
            description="d" * 400,
            color=RED,
            fields=[("Reason", "stalled"), ("Empty", "")],
            url="https://x/movie/1",
            image="https://img/p.jpg",
        ),
    )

    assert [u for u, _ in posted] == [
        "https://discord.com/api/webhooks/1/abc",
        "https://discord.com/api/webhooks/2/def",
    ]
    embed = posted[0][1]["embeds"][0]
    assert embed["author"] == {"name": "Uncached Download Failed"}
    assert embed["title"] == "Movie (2020)"
    assert embed["url"] == "https://x/movie/1"
    assert embed["color"] == RED
    assert embed["thumbnail"] == {"url": "https://img/p.jpg"}
    assert len(embed["description"]) == notify.DESCRIPTION_LIMIT
    assert embed["fields"] == [{"name": "Reason", "value": "stalled", "inline": True}]
    assert apprised == [
        (["ntfy://topic"], "Uncached Download Failed: Movie (2020)", "d" * 400 + "\nReason: stalled")
    ]
