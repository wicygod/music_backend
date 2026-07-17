from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.album import Album, AlbumTrack
from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.services.normalization_service import (
    all_search_tokens_match,
    compact_search_text,
    normalize_search_text,
    normalize_title,
    search_candidate_fragments,
    search_tokens,
)


def with_album_relations(stmt):
    return stmt.options(
        selectinload(Album.artist),
        selectinload(Album.track_links)
        .selectinload(AlbumTrack.track)
        .selectinload(Track.artist_links)
        .selectinload(TrackArtist.artist),
        selectinload(Album.track_links)
        .selectinload(AlbumTrack.track)
        .selectinload(Track.album_links)
        .selectinload(AlbumTrack.album),
    )


def get_album(db: Session, album_id: int) -> Album | None:
    return db.execute(with_album_relations(select(Album).where(Album.id == album_id))).scalars().unique().first()


def list_artist_albums(db: Session, artist_id: int, limit: int = 100) -> list[Album]:
    stmt = (
        with_album_relations(select(Album).where(Album.artist_id == artist_id, Album.is_available == True))
        .order_by(Album.release_date.desc(), Album.popularity_score.desc(), Album.id.desc())
        .limit(max(1, min(int(limit), 100)))
    )
    return list(db.execute(stmt).scalars().unique().all())


def album_refresh_due(db: Session, artist_id: int, ttl_hours: int = 24) -> bool:
    latest = db.execute(select(func.max(Album.updated_at)).where(Album.artist_id == artist_id)).scalar_one_or_none()
    return latest is None or latest < datetime.utcnow() - timedelta(hours=max(1, int(ttl_hours)))


def find_album_by_source(db: Session, source_name: str, source_external_id: str) -> Album | None:
    if not source_name or not source_external_id:
        return None
    return db.execute(
        select(Album).where(
            Album.source_name == source_name,
            Album.source_external_id == source_external_id,
        )
    ).scalars().first()


def upsert_album(
    db: Session,
    *,
    artist: Artist,
    title: str,
    album_type: str,
    cover_url: str | None,
    release_date: datetime | None,
    track_count: int,
    source_name: str,
    source_external_id: str,
    source_url: str,
    popularity_score: float,
) -> tuple[Album, bool]:
    album = find_album_by_source(db, source_name, source_external_id)
    created = album is None
    if album is None:
        album = Album(
            artist_id=artist.id,
            title=title.strip(),
            normalized_title=normalize_title(title),
            album_type=album_type,
            cover_url=cover_url,
            release_date=release_date,
            track_count=max(0, int(track_count)),
            source_name=source_name,
            source_external_id=source_external_id,
            source_url=source_url,
            popularity_score=max(0.0, float(popularity_score)),
            is_available=True,
        )
        db.add(album)
        db.flush()
        return album, created

    album.artist_id = artist.id
    album.title = title.strip()
    album.normalized_title = normalize_title(title)
    album.album_type = album_type
    album.cover_url = cover_url or album.cover_url
    album.release_date = release_date or album.release_date
    album.track_count = max(0, int(track_count))
    album.source_url = source_url
    album.popularity_score = max(0.0, float(popularity_score))
    album.is_available = True
    return album, created


def link_album_track(
    db: Session,
    *,
    album_id: int,
    track_id: int,
    track_number: int,
    disc_number: int = 1,
) -> AlbumTrack:
    existing = db.get(AlbumTrack, {"album_id": album_id, "track_id": track_id})
    if existing is not None:
        existing.disc_number = max(1, int(disc_number))
        existing.track_number = max(1, int(track_number))
        return existing
    position_conflict = db.execute(
        select(AlbumTrack).where(
            AlbumTrack.album_id == album_id,
            AlbumTrack.disc_number == max(1, int(disc_number)),
            AlbumTrack.track_number == max(1, int(track_number)),
        )
    ).scalars().first()
    if position_conflict is not None:
        db.delete(position_conflict)
        db.flush()
    link = AlbumTrack(
        album_id=album_id,
        track_id=track_id,
        disc_number=max(1, int(disc_number)),
        track_number=max(1, int(track_number)),
    )
    db.add(link)
    db.flush()
    return link


def search_album_matches(db: Session, query: str, limit: int = 3) -> list[tuple[Album, int | None]]:
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return []

    def escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    safe_limit = max(1, min(int(limit), 10))
    tokens = search_tokens(query)
    compact_query = compact_search_text(query)
    def compact_sql(column):
        compacted = column
        for character in (" ", ".", ",", "-", "_", "'", "/", "&"):
            compacted = func.replace(compacted, character, "")
        return compacted

    compact_album_title = compact_sql(Album.normalized_title)
    compact_track_title = compact_sql(Track.normalized_title)
    compact_artist_name = compact_sql(Artist.normalized_name)
    token_filters = [
        or_(
            Album.normalized_title.like(f"%{escape_like(token)}%", escape="\\"),
            Track.normalized_title.like(f"%{escape_like(token)}%", escape="\\"),
            Artist.normalized_name.like(f"%{escape_like(token)}%", escape="\\"),
        )
        for token in tokens
    ]
    direct_match = and_(*token_filters) if token_filters else False
    if len(compact_query) >= 3:
        direct_match = or_(
            direct_match,
            compact_album_title == compact_query,
            compact_track_title == compact_query,
            compact_artist_name == compact_query,
        )
    direct_ids = list(
        db.execute(
            select(Album.id)
            .outerjoin(AlbumTrack, AlbumTrack.album_id == Album.id)
            .outerjoin(Track, Track.id == AlbumTrack.track_id)
            .join(Artist, Artist.id == Album.artist_id)
            .where(
                Album.is_available == True,
                direct_match,
            )
            .distinct()
            .limit(max(20, min(safe_limit * 12, 80)))
        ).scalars().all()
    )
    album_ids = list(direct_ids)
    if len(album_ids) < safe_limit:
        fragment_filters = []
        for fragment in search_candidate_fragments(query):
            pattern = f"%{escape_like(fragment)}%"
            fragment_filters.extend(
                (
                    compact_album_title.like(pattern, escape="\\"),
                    compact_track_title.like(pattern, escape="\\"),
                    compact_artist_name.like(pattern, escape="\\"),
                )
            )
        if fragment_filters:
            fuzzy_ids = list(
                db.execute(
                    select(Album.id, Album.popularity_score)
                    .outerjoin(AlbumTrack, AlbumTrack.album_id == Album.id)
                    .outerjoin(Track, Track.id == AlbumTrack.track_id)
                    .join(Artist, Artist.id == Album.artist_id)
                    .where(
                        Album.is_available == True,
                        or_(*fragment_filters),
                    )
                    .order_by(Album.popularity_score.desc(), Album.id.asc())
                    .distinct()
                    .limit(80)
                ).all()
            )
            album_ids.extend(
                int(album_id)
                for album_id, _popularity_score in fuzzy_ids
                if int(album_id) not in album_ids
            )
    if not album_ids:
        return []
    albums = list(
        db.execute(with_album_relations(select(Album).where(Album.id.in_(album_ids)))).scalars().unique().all()
    )
    ranked: list[tuple[tuple[int, float, float, int], Album, int | None]] = []
    for album in albums:
        matched_track_id = None
        best_track_rank = 9
        playable_links = [
            link
            for link in album.track_links
            if link.track.is_playable
            and not link.track.needs_review
            and (
                link.track.audio_src
                or (
                    link.track.source_url
                    and (link.track.source_name or "").lower()
                    in {"soundcloud", "sc", "youtube", "youtube_music", "yt"}
                )
            )
        ]
        if not playable_links:
            continue
        for link in playable_links:
            rank = _album_entity_match_rank(
                query,
                link.track.title,
                album.artist.name,
            )
            if rank < best_track_rank:
                best_track_rank = rank
                matched_track_id = link.track_id
        album_rank = _album_entity_match_rank(query, album.title, album.artist.name)
        match_rank = min(best_track_rank, album_rank)
        if match_rank >= 9:
            continue
        release_value = album.release_date.timestamp() if album.release_date else 0.0
        ranked.append(
            ((match_rank, -float(album.popularity_score or 0.0), -release_value, album.id), album, matched_track_id)
        )
    ranked.sort(key=lambda item: item[0])
    return [(album, matched_track_id) for _rank, album, matched_track_id in ranked[:safe_limit]]


def _album_entity_match_rank(query: str, title: str, artist: str) -> int:
    normalized_query = normalize_search_text(query)
    normalized_title = normalize_search_text(title)
    compact_query = compact_search_text(query)
    if normalized_title == normalized_query or (
        len(compact_query) >= 3
        and compact_search_text(title) == compact_query
    ):
        return 0
    tokens = search_tokens(query)
    if all_search_tokens_match(tokens, normalized_title):
        return 1
    combined = f"{artist} {title}"
    if (
        (
            len(compact_query) >= 3
            and compact_query in compact_search_text(combined)
        )
        or all_search_tokens_match(tokens, normalize_search_text(combined))
    ):
        return 2
    return 9
