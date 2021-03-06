# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import copy
import enum
import os
import pprint
import re
import sys
import time
import traceback
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Tuple, Union

import anki._backend.backend_pb2 as _pb
import anki.find
import anki.latex  # sets up hook
import anki.template
from anki import hooks
from anki._backend import RustBackend
from anki.cards import Card
from anki.config import ConfigManager
from anki.consts import *
from anki.dbproxy import DBProxy
from anki.decks import DeckManager
from anki.errors import AnkiError, DBError
from anki.lang import TR, FormatTimeSpanContext
from anki.media import MediaManager, media_paths_from_col_path
from anki.models import ModelManager
from anki.notes import Note
from anki.sched import Scheduler as V1Scheduler
from anki.schedv2 import Scheduler as V2Scheduler
from anki.sync import SyncAuth, SyncOutput, SyncStatus
from anki.tags import TagManager
from anki.utils import (
    devMode,
    from_json_bytes,
    ids2str,
    intTime,
    splitFields,
    stripHTMLMedia,
)

# public exports
SearchTerm = _pb.SearchTerm
MediaSyncProgress = _pb.MediaSyncProgress
FullSyncProgress = _pb.FullSyncProgress
NormalSyncProgress = _pb.NormalSyncProgress
DatabaseCheckProgress = _pb.DatabaseCheckProgress
ConfigBoolKey = _pb.ConfigBool.Key  # pylint: disable=no-member
EmptyCardsReport = _pb.EmptyCardsReport
NoteWithEmptyCards = _pb.NoteWithEmptyCards
GraphPreferences = _pb.GraphPreferences
BuiltinSortKind = _pb.SortOrder.Builtin.Kind  # pylint: disable=no-member
Preferences = _pb.Preferences

# pylint: disable=no-member
if TYPE_CHECKING:
    from anki.lang import FormatTimeSpanContextValue, TRValue

    ConfigBoolKeyValue = _pb.ConfigBool.KeyValue
    BuiltinSortKindValue = _pb.SortOrder.Builtin.KindValue


class Collection:
    sched: Union[V1Scheduler, V2Scheduler]
    _undo: List[Any]

    def __init__(
        self,
        path: str,
        backend: Optional[RustBackend] = None,
        server: bool = False,
        log: bool = False,
    ) -> None:
        self._backend = backend or RustBackend(server=server)
        self.db: Optional[DBProxy] = None
        self._should_log = log
        self.server = server
        self.path = os.path.abspath(path)
        self.reopen()

        self.log(self.path, anki.version)
        self._lastSave = time.time()
        self.clearUndo()
        self.media = MediaManager(self, server)
        self.models = ModelManager(self)
        self.decks = DeckManager(self)
        self.tags = TagManager(self)
        self.conf = ConfigManager(self)
        self._loadScheduler()

    def __repr__(self) -> str:
        d = dict(self.__dict__)
        del d["models"]
        del d["backend"]
        return f"{super().__repr__()} {pprint.pformat(d, width=300)}"

    def name(self) -> Any:
        return os.path.splitext(os.path.basename(self.path))[0]

    def weakref(self) -> Collection:
        "Shortcut to create a weak reference that doesn't break code completion."
        return weakref.proxy(self)

    @property
    def backend(self) -> RustBackend:
        traceback.print_stack(file=sys.stdout)
        print()
        print(
            "Accessing the backend directly will break in the future. Please use the public methods on Collection instead."
        )
        return self._backend

    # I18n/messages
    ##########################################################################

    def tr(self, key: TRValue, **kwargs: Union[str, int, float]) -> str:
        return self._backend.translate(key, **kwargs)

    def format_timespan(
        self,
        seconds: float,
        context: FormatTimeSpanContextValue = FormatTimeSpanContext.INTERVALS,
    ) -> str:
        return self._backend.format_timespan(seconds=seconds, context=context)

    # Progress
    ##########################################################################

    def latest_progress(self) -> Progress:
        return Progress.from_proto(self._backend.latest_progress())

    # Scheduler
    ##########################################################################

    supportedSchedulerVersions = (1, 2)

    def schedVer(self) -> Any:
        ver = self.conf.get("schedVer", 1)
        if ver in self.supportedSchedulerVersions:
            return ver
        else:
            raise Exception("Unsupported scheduler version")

    def _loadScheduler(self) -> None:
        ver = self.schedVer()
        if ver == 1:
            self.sched = V1Scheduler(self)
        elif ver == 2:
            self.sched = V2Scheduler(self)

    def changeSchedulerVer(self, ver: int) -> None:
        if ver == self.schedVer():
            return
        if ver not in self.supportedSchedulerVersions:
            raise Exception("Unsupported scheduler version")

        self.modSchema(check=True)
        self.clearUndo()

        v2Sched = V2Scheduler(self)

        if ver == 1:
            v2Sched.moveToV1()
        else:
            v2Sched.moveToV2()

        self.conf["schedVer"] = ver
        self.setMod()

        self._loadScheduler()

    # DB-related
    ##########################################################################

    # legacy properties; these will likely go away in the future

    def _get_crt(self) -> int:
        return self.db.scalar("select crt from col")

    def _set_crt(self, val: int) -> None:
        self.db.execute("update col set crt=?", val)

    def _get_scm(self) -> int:
        return self.db.scalar("select scm from col")

    def _set_scm(self, val: int) -> None:
        self.db.execute("update col set scm=?", val)

    def _get_usn(self) -> int:
        return self.db.scalar("select usn from col")

    def _set_usn(self, val: int) -> None:
        self.db.execute("update col set usn=?", val)

    def _get_mod(self) -> int:
        return self.db.scalar("select mod from col")

    def _set_mod(self, val: int) -> None:
        self.db.execute("update col set mod=?", val)

    def _get_ls(self) -> int:
        return self.db.scalar("select ls from col")

    def _set_ls(self, val: int) -> None:
        self.db.execute("update col set ls=?", val)

    crt = property(_get_crt, _set_crt)
    mod = property(_get_mod, _set_mod)
    _usn = property(_get_usn, _set_usn)
    scm = property(_get_scm, _set_scm)
    ls = property(_get_ls, _set_ls)

    # legacy
    def setMod(self, mod: Optional[int] = None) -> None:
        # this is now a no-op, as modifications to things like the config
        # will mark the collection modified automatically
        pass

    flush = setMod

    def modified_after_begin(self) -> bool:
        # Until we can move away from long-running transactions, the Python
        # code needs to know if transaction should be committed, so we need
        # to check if the backend updated the modification time.
        return self.db.last_begin_at != self.mod

    def save(
        self, name: Optional[str] = None, mod: Optional[int] = None, trx: bool = True
    ) -> None:
        "Flush, commit DB, and take out another write lock if trx=True."
        # commit needed?
        if self.db.mod or self.modified_after_begin():
            self.mod = intTime(1000) if mod is None else mod
            self.db.commit()
            self.db.mod = False
            if trx:
                self.db.begin()
        elif not trx:
            # if no changes were pending but calling code expects to be
            # outside of a transaction, we need to roll back
            self.db.rollback()

        self._markOp(name)
        self._lastSave = time.time()

    def autosave(self) -> Optional[bool]:
        "Save if 5 minutes has passed since last save. True if saved."
        if time.time() - self._lastSave > 300:
            self.save()
            return True
        return None

    def close(self, save: bool = True, downgrade: bool = False) -> None:
        "Disconnect from DB."
        if self.db:
            if save:
                self.save(trx=False)
            else:
                self.db.rollback()
            self.models._clear_cache()
            self._backend.close_collection(downgrade_to_schema11=downgrade)
            self.db = None
            self.media.close()
            self._closeLog()

    def close_for_full_sync(self) -> None:
        # save and cleanup, but backend will take care of collection close
        if self.db:
            self.save(trx=False)
            self.models._clear_cache()
            self.db = None
            self.media.close()
            self._closeLog()

    def rollback(self) -> None:
        self.db.rollback()
        self.db.begin()

    def reopen(self, after_full_sync: bool = False) -> None:
        assert not self.db
        assert self.path.endswith(".anki2")

        (media_dir, media_db) = media_paths_from_col_path(self.path)

        log_path = ""
        should_log = not self.server and self._should_log
        if should_log:
            log_path = self.path.replace(".anki2", "2.log")

        # connect
        if not after_full_sync:
            self._backend.open_collection(
                collection_path=self.path,
                media_folder_path=media_dir,
                media_db_path=media_db,
                log_path=log_path,
            )
        else:
            self.media.connect()
        self.db = DBProxy(weakref.proxy(self._backend))
        self.db.begin()

        self._openLog()

    def modSchema(self, check: bool) -> None:
        "Mark schema modified. Call this first so user can abort if necessary."
        if not self.schemaChanged():
            if check and not hooks.schema_will_change(proceed=True):
                raise AnkiError("abortSchemaMod")
        self.scm = intTime(1000)
        self.setMod()
        self.save()

    def schemaChanged(self) -> Any:
        "True if schema changed since last sync."
        return self.scm > self.ls

    def usn(self) -> Any:
        return self._usn if self.server else -1

    def beforeUpload(self) -> None:
        "Called before a full upload."
        self.save(trx=False)
        self._backend.before_upload()
        self.close(save=False, downgrade=True)

    # Object creation helpers
    ##########################################################################

    def getCard(self, id: int) -> Card:
        return Card(self, id)

    def getNote(self, id: int) -> Note:
        return Note(self, id=id)

    # Utils
    ##########################################################################

    def nextID(self, type: str, inc: bool = True) -> Any:
        type = "next" + type.capitalize()
        id = self.conf.get(type, 1)
        if inc:
            self.conf[type] = id + 1
        return id

    def reset(self) -> None:
        "Rebuild the queue and reload data after DB modified."
        self.sched.reset()

    # Deletion logging
    ##########################################################################

    def _logRem(self, ids: List[int], type: int) -> None:
        self.db.executemany(
            "insert into graves values (%d, ?, %d)" % (self.usn(), type),
            ([x] for x in ids),
        )

    # Notes
    ##########################################################################

    def noteCount(self) -> Any:
        return self.db.scalar("select count() from notes")

    def newNote(self, forDeck: bool = True) -> Note:
        "Return a new note with the current model."
        return Note(self, self.models.current(forDeck))

    def add_note(self, note: Note, deck_id: int) -> None:
        note.id = self._backend.add_note(note=note.to_backend_note(), deck_id=deck_id)

    def remove_notes(self, note_ids: Sequence[int]) -> None:
        hooks.notes_will_be_deleted(self, note_ids)
        self._backend.remove_notes(note_ids=note_ids, card_ids=[])

    def remove_notes_by_card(self, card_ids: List[int]) -> None:
        if hooks.notes_will_be_deleted.count():
            nids = self.db.list(
                "select nid from cards where id in " + ids2str(card_ids)
            )
            hooks.notes_will_be_deleted(self, nids)
        self._backend.remove_notes(note_ids=[], card_ids=card_ids)

    def card_ids_of_note(self, note_id: int) -> Sequence[int]:
        return self._backend.cards_of_note(note_id)

    # legacy

    def addNote(self, note: Note) -> int:
        self.add_note(note, note.model()["did"])
        return len(note.cards())

    def remNotes(self, ids: Sequence[int]) -> None:
        self.remove_notes(ids)

    def _remNotes(self, ids: List[int]) -> None:
        pass

    # Cards
    ##########################################################################

    def isEmpty(self) -> bool:
        return not self.db.scalar("select 1 from cards limit 1")

    def cardCount(self) -> Any:
        return self.db.scalar("select count() from cards")

    def remove_cards_and_orphaned_notes(self, card_ids: Sequence[int]) -> None:
        "You probably want .remove_notes_by_card() instead."
        self._backend.remove_cards(card_ids=card_ids)

    def set_deck(self, card_ids: List[int], deck_id: int) -> None:
        self._backend.set_deck(card_ids=card_ids, deck_id=deck_id)

    def get_empty_cards(self) -> EmptyCardsReport:
        return self._backend.get_empty_cards()

    # legacy

    def remCards(self, ids: List[int], notes: bool = True) -> None:
        self.remove_cards_and_orphaned_notes(ids)

    def emptyCids(self) -> List[int]:
        print("emptyCids() will go away")
        return []

    # Card generation & field checksums/sort fields
    ##########################################################################

    def after_note_updates(
        self, nids: List[int], mark_modified: bool, generate_cards: bool = True
    ) -> None:
        self._backend.after_note_updates(
            nids=nids, generate_cards=generate_cards, mark_notes_modified=mark_modified
        )

    # legacy

    def updateFieldCache(self, nids: List[int]) -> None:
        self.after_note_updates(nids, mark_modified=False, generate_cards=False)

    # this also updates field cache
    def genCards(self, nids: List[int]) -> List[int]:
        self.after_note_updates(nids, mark_modified=False, generate_cards=True)
        # previously returned empty cards, no longer does
        return []

    # Finding cards
    ##########################################################################

    # if order=True, use the sort order stored in the collection config
    # if order=False, do no ordering
    #
    # if order is a string, that text is added after 'order by' in the sql statement.
    # you must add ' asc' or ' desc' to the order, as Anki will replace asc with
    # desc and vice versa when reverse is set in the collection config, eg
    # order="c.ivl asc, c.due desc"
    #
    # if order is an int enum, sort using that builtin sort, eg
    # col.find_cards("", order=BuiltinSortKind.CARD_DUE)
    # the reverse argument only applies when a BuiltinSortKind is provided;
    # otherwise the collection config defines whether reverse is set or not
    def find_cards(
        self,
        query: str,
        order: Union[bool, str, BuiltinSortKindValue] = False,
        reverse: bool = False,
    ) -> Sequence[int]:
        if isinstance(order, str):
            mode = _pb.SortOrder(custom=order)
        elif isinstance(order, bool):
            if order is True:
                mode = _pb.SortOrder(from_config=_pb.Empty())
            else:
                mode = _pb.SortOrder(none=_pb.Empty())
        else:
            mode = _pb.SortOrder(
                builtin=_pb.SortOrder.Builtin(kind=order, reverse=reverse)
            )
        return self._backend.search_cards(search=query, order=mode)

    def find_notes(self, *terms: Union[str, SearchTerm]) -> Sequence[int]:
        return self._backend.search_notes(self.build_search_string(*terms))

    def find_and_replace(
        self,
        nids: List[int],
        src: str,
        dst: str,
        regex: Optional[bool] = None,
        field: Optional[str] = None,
        fold: bool = True,
    ) -> int:
        return anki.find.findReplace(self, nids, src, dst, regex, field, fold)

    # returns array of ("dupestr", [nids])
    def findDupes(self, fieldName: str, search: str = "") -> List[Tuple[Any, list]]:
        nids = self.findNotes(search, SearchTerm(field_name=fieldName))
        # go through notes
        vals: Dict[str, List[int]] = {}
        dupes = []
        fields: Dict[int, int] = {}

        def ordForMid(mid: int) -> int:
            if mid not in fields:
                model = self.models.get(mid)
                for c, f in enumerate(model["flds"]):
                    if f["name"].lower() == fieldName.lower():
                        fields[mid] = c
                        break
            return fields[mid]

        for nid, mid, flds in self.db.all(
            "select id, mid, flds from notes where id in " + ids2str(nids)
        ):
            flds = splitFields(flds)
            ord = ordForMid(mid)
            if ord is None:
                continue
            val = flds[ord]
            val = stripHTMLMedia(val)
            # empty does not count as duplicate
            if not val:
                continue
            vals.setdefault(val, []).append(nid)
            if len(vals[val]) == 2:
                dupes.append((val, vals[val]))
        return dupes

    findCards = find_cards
    findNotes = find_notes
    findReplace = find_and_replace

    # Search Strings
    ##########################################################################

    def build_search_string(
        self,
        *terms: Union[str, SearchTerm],
        negate: bool = False,
        match_any: bool = False,
    ) -> str:
        """Helper function for the backend's search string operations.

        Pass terms as strings to normalize.
        Pass fields of backend.proto/FilterToSearchIn as valid SearchTerms.
        Pass multiple terms to concatenate (defaults to 'and', 'or' when 'match_any=True').
        Pass 'negate=True' to negate the end result.
        May raise InvalidInput.
        """

        searches = []
        for term in terms:
            if isinstance(term, SearchTerm):
                term = self._backend.filter_to_search(term)
            searches.append(term)
        if match_any:
            sep = _pb.ConcatenateSearchesIn.Separator.OR
        else:
            sep = _pb.ConcatenateSearchesIn.Separator.AND
        search_string = self._backend.concatenate_searches(sep=sep, searches=searches)
        if negate:
            search_string = self._backend.negate_search(search_string)
        return search_string

    def replace_search_term(self, search: str, replacement: str) -> str:
        return self._backend.replace_search_term(search=search, replacement=replacement)

    # Config
    ##########################################################################

    def get_config(self, key: str, default: Any = None) -> Any:
        try:
            return self.conf.get_immutable(key)
        except KeyError:
            return default

    def set_config(self, key: str, val: Any) -> None:
        self.setMod()
        self.conf.set(key, val)

    def remove_config(self, key: str) -> None:
        self.setMod()
        self.conf.remove(key)

    def all_config(self) -> Dict[str, Any]:
        "This is a debugging aid. Prefer .get_config() when you know the key you need."
        return from_json_bytes(self._backend.get_all_config())

    def get_config_bool(self, key: ConfigBoolKeyValue) -> bool:
        return self._backend.get_config_bool(key)

    def set_config_bool(self, key: ConfigBoolKeyValue, value: bool) -> None:
        self.setMod()
        self._backend.set_config_bool(key=key, value=value)

    # Stats
    ##########################################################################

    def stats(self) -> "anki.stats.CollectionStats":
        from anki.stats import CollectionStats

        return CollectionStats(self)

    def card_stats(self, card_id: int, include_revlog: bool) -> str:
        import anki.stats as st

        if include_revlog:
            revlog_style = "margin-top: 2em;"
        else:
            revlog_style = "display: none;"

        style = f"""<style>
.revlog-learn {{ color: {st.colLearn} }}
.revlog-review {{ color: {st.colMature} }}
.revlog-relearn {{ color: {st.colRelearn} }}
.revlog-ease1 {{ color: {st.colRelearn} }}
table.review-log {{ {revlog_style} }}
</style>"""

        return style + self._backend.card_stats(card_id)

    def studied_today(self) -> str:
        return self._backend.studied_today()

    def graph_data(self, search: str, days: int) -> bytes:
        return self._backend.graphs(search=search, days=days)

    def get_graph_preferences(self) -> bytes:
        return self._backend.get_graph_preferences()

    def set_graph_preferences(self, prefs: GraphPreferences) -> None:
        self._backend.set_graph_preferences(input=prefs)

    def congrats_info(self) -> bytes:
        "Don't use this, it will likely go away in the future."
        return self._backend.congrats_info().SerializeToString()

    # legacy

    def cardStats(self, card: Card) -> str:
        return self.card_stats(card.id, include_revlog=False)

    # Timeboxing
    ##########################################################################

    def startTimebox(self) -> None:
        self._startTime = time.time()
        self._startReps = self.sched.reps

    # FIXME: Use Literal[False] when on Python 3.8
    def timeboxReached(self) -> Union[bool, Tuple[Any, int]]:
        "Return (elapsedTime, reps) if timebox reached, or False."
        if not self.conf["timeLim"]:
            # timeboxing disabled
            return False
        elapsed = time.time() - self._startTime
        if elapsed > self.conf["timeLim"]:
            return (self.conf["timeLim"], self.sched.reps - self._startReps)
        return False

    # Undo
    ##########################################################################
    # this data structure is a mess, and will be updated soon
    # in the review case, [1, "Review", [firstReviewedCard, secondReviewedCard, ...], wasLeech]
    # in the checkpoint case, [2, "action name"]
    # wasLeech should have been recorded for each card, not globally

    def clearUndo(self) -> None:
        self._undo = None

    def undoName(self) -> Any:
        "Undo menu item name, or None if undo unavailable."
        if not self._undo:
            return None
        return self._undo[1]

    def undo(self) -> Any:
        if self._undo[0] == 1:
            return self._undoReview()
        else:
            self._undoOp()

    def markReview(self, card: Card) -> None:
        old: List[Any] = []
        if self._undo:
            if self._undo[0] == 1:
                old = self._undo[2]
            self.clearUndo()
        wasLeech = card.note().hasTag("leech") or False
        self._undo = [
            1,
            self.tr(TR.SCHEDULING_REVIEW),
            old + [copy.copy(card)],
            wasLeech,
        ]

    def _undoReview(self) -> Any:
        data = self._undo[2]
        wasLeech = self._undo[3]
        c = data.pop()  # pytype: disable=attribute-error
        if not data:
            self.clearUndo()
        # remove leech tag if it didn't have it before
        if not wasLeech and c.note().hasTag("leech"):
            c.note().delTag("leech")
            c.note().flush()
        # write old data
        c.flush()
        # and delete revlog entry if not previewing
        conf = self.sched._cardConf(c)
        previewing = conf["dyn"] and not conf["resched"]
        if not previewing:
            last = self.db.scalar(
                "select id from revlog where cid = ? " "order by id desc limit 1", c.id
            )
            self.db.execute("delete from revlog where id = ?", last)
        # restore any siblings
        self.db.execute(
            "update cards set queue=type,mod=?,usn=? where queue=-2 and nid=?",
            intTime(),
            self.usn(),
            c.nid,
        )
        # and finally, update daily counts
        n = c.queue
        if c.queue in (QUEUE_TYPE_DAY_LEARN_RELEARN, QUEUE_TYPE_PREVIEW):
            n = QUEUE_TYPE_LRN
        type = ("new", "lrn", "rev")[n]
        self.sched._updateStats(c, type, -1)
        self.sched.reps -= 1
        return c.id

    def _markOp(self, name: Optional[str]) -> None:
        "Call via .save()"
        if name:
            self._undo = [2, name]
        else:
            # saving disables old checkpoint, but not review undo
            if self._undo and self._undo[0] == 2:
                self.clearUndo()

    def _undoOp(self) -> None:
        self.rollback()
        self.clearUndo()

    # DB maintenance
    ##########################################################################

    def fixIntegrity(self) -> Tuple[str, bool]:
        """Fix possible problems and rebuild caches.

        Returns tuple of (error: str, ok: bool). 'ok' will be true if no
        problems were found.
        """
        self.save(trx=False)
        try:
            problems = list(self._backend.check_database())
            ok = not problems
            problems.append(self.tr(TR.DATABASE_CHECK_REBUILT))
        except DBError as e:
            problems = [str(e.args[0])]
            ok = False
        finally:
            try:
                self.db.begin()
            except:
                # may fail if the DB is very corrupt
                pass
        return ("\n".join(problems), ok)

    def optimize(self) -> None:
        self.save(trx=False)
        self.db.execute("vacuum")
        self.db.execute("analyze")
        self.db.begin()

    # Logging
    ##########################################################################

    def log(self, *args: Any, **kwargs: Any) -> None:
        if not self._should_log:
            return

        def customRepr(x: Any) -> str:
            if isinstance(x, str):
                return x
            return pprint.pformat(x)

        path, num, fn, y = traceback.extract_stack(limit=2 + kwargs.get("stack", 0))[0]
        buf = "[%s] %s:%s(): %s" % (
            intTime(),
            os.path.basename(path),
            fn,
            ", ".join([customRepr(x) for x in args]),
        )
        self._logHnd.write(buf + "\n")
        if devMode:
            print(buf)

    def _openLog(self) -> None:
        if not self._should_log:
            return
        lpath = re.sub(r"\.anki2$", ".log", self.path)
        if os.path.exists(lpath) and os.path.getsize(lpath) > 10 * 1024 * 1024:
            lpath2 = lpath + ".old"
            if os.path.exists(lpath2):
                os.unlink(lpath2)
            os.rename(lpath, lpath2)
        self._logHnd = open(lpath, "a", encoding="utf8")

    def _closeLog(self) -> None:
        if not self._should_log:
            return
        self._logHnd.close()
        self._logHnd = None

    # Card Flags
    ##########################################################################

    def setUserFlag(self, flag: int, cids: List[int]) -> None:
        assert 0 <= flag <= 7
        self.db.execute(
            "update cards set flags = (flags & ~?) | ?, usn=?, mod=? where id in %s"
            % ids2str(cids),
            0b111,
            flag,
            self.usn(),
            intTime(),
        )

    ##########################################################################

    def set_wants_abort(self) -> None:
        self._backend.set_wants_abort()

    def i18n_resources(self) -> bytes:
        return self._backend.i18n_resources()

    def abort_media_sync(self) -> None:
        self._backend.abort_media_sync()

    def abort_sync(self) -> None:
        self._backend.abort_sync()

    def full_upload(self, auth: SyncAuth) -> None:
        self._backend.full_upload(auth)

    def full_download(self, auth: SyncAuth) -> None:
        self._backend.full_download(auth)

    def sync_login(self, username: str, password: str) -> SyncAuth:
        return self._backend.sync_login(username=username, password=password)

    def sync_collection(self, auth: SyncAuth) -> SyncOutput:
        return self._backend.sync_collection(auth)

    def sync_media(self, auth: SyncAuth) -> None:
        self._backend.sync_media(auth)

    def sync_status(self, auth: SyncAuth) -> SyncStatus:
        return self._backend.sync_status(auth)

    def get_preferences(self) -> Preferences:
        return self._backend.get_preferences()

    def set_preferences(self, prefs: Preferences) -> None:
        self._backend.set_preferences(prefs)


class ProgressKind(enum.Enum):
    NoProgress = 0
    MediaSync = 1
    MediaCheck = 2
    FullSync = 3
    NormalSync = 4
    DatabaseCheck = 5


@dataclass
class Progress:
    kind: ProgressKind
    val: Union[
        MediaSyncProgress,
        FullSyncProgress,
        NormalSyncProgress,
        DatabaseCheckProgress,
        str,
    ]

    @staticmethod
    def from_proto(proto: _pb.Progress) -> Progress:
        kind = proto.WhichOneof("value")
        if kind == "media_sync":
            return Progress(kind=ProgressKind.MediaSync, val=proto.media_sync)
        elif kind == "media_check":
            return Progress(kind=ProgressKind.MediaCheck, val=proto.media_check)
        elif kind == "full_sync":
            return Progress(kind=ProgressKind.FullSync, val=proto.full_sync)
        elif kind == "normal_sync":
            return Progress(kind=ProgressKind.NormalSync, val=proto.normal_sync)
        elif kind == "database_check":
            return Progress(kind=ProgressKind.DatabaseCheck, val=proto.database_check)
        else:
            return Progress(kind=ProgressKind.NoProgress, val="")


# legacy name
_Collection = Collection
