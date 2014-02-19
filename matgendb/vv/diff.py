"""
Diff collections, as sets
"""
__author__ = 'Dan Gunter <dkgunter@lbl.gov>'
__date__ = '3/29/13'

import logging
import re
import time
from matgendb import util
from matgendb.query_engine import QueryEngine

_log = logging.getLogger("mg.vv.diff")


class Differ(object):
    """Calculate difference between two collections, based solely on a
    selected key.

    As noted in :func:`diff`, this will not work with huge datasets, as it stores
    all the keys in memory in order to do a "set difference" using Python sets.
    """

    #: Keys in result dictionary.
    MISSING, NEW, CHANGED = 'missing', 'additional', 'different'

    #: for missing property
    NO_PROPERTY = "__MISSING__"

    def __init__(self, key='_id', props=None, info=None, fltr=None, deltas=None):
        """Constructor.

        :param key: Field to use for identifying records
        :param props: List of fields to use for matching records
        :param info: List of extra fields to retrieve from (and show) for each record.
        :param fltr: Filter for records, a MongoDB query expression
        :param deltas: {prop: delta} to check. 'prop' is a string, 'delta' is an instance of :class:`Delta`.
                       Any key for 'prop' not in parameter 'props' will get added.
        :type deltas: dict
        :raise: ValueError if some delta does not parse.
        """
        self._key_field = key
        self._props = [] if props is None else props
        self._info = [] if info is None else info
        self._filter = fltr if fltr else {}
        self._prop_deltas = {} if deltas is None else deltas
        self._all_props = list(set(self._props[:] + self._prop_deltas.keys()))

    def diff(self, c1, c2, only_missing=False, allow_dup=False):
        """Perform a difference between the 2 collections.
        The first collection is treated as the previous one, and the second
        is treated as the new one.

        Note: this is not 'big data'-ready; we assume all the records can fit in memory.

        :param c1: Collection (1) config file, or QueryEngine object
        :type c1: str or QueryEngine
        :param c2: Collection (2) config file, or QueryEngine object
        :type c2: str or QueryEngine
        :param only_missing: Only find and return self.MISSING; ignore 'new' keys
        :param allow_dup: Allow duplicate keys, otherwise fail with ValueError
        :return: dict with keys self.MISSING, self.NEW (unless only_missing is True), & self.CHANGED,
                 each a list of records with the key and
                 any other fields given to the constructor 'info' argument.
                 The meaning is: 'missing' are keys that are in c1 not found in c2
                 'new' is keys found in c2 that are not found in c1, and 'changed' are records
                 with the same key that have different 'props' values.
        """
        # Connect.
        _log.info("connect.start")
        if isinstance(c1, QueryEngine):
            engines = [c1, c2]
        else:
            engines = []
            for cfg in c1, c2:
                settings = util.get_settings(cfg)
                if not util.normalize_auth(settings):
                    _log.warn("Config file {} does not have a username/password".format(cfg))
                settings["aliases_config"] = {"aliases": {}, "defaults": {}}
                engine = QueryEngine(**settings)
                engines.append(engine)
        _log.info("connect.end")

        # Query DB.
        keys = [set(), set()]
        eqprops = [{}, {}]
        numprops = [{}, {}]

        # Build query fields.
        fields = dict.fromkeys(self._info + self._all_props + [self._key_field], True)
        if not '_id' in fields:  # explicitly remove _id if not given
            fields['_id'] = False

        # Initialize for query loop.
        info = {}  # per-key information
        has_info, has_props = bool(self._info), bool(self._all_props)
        has_numprops, has_eqprops = bool(self._prop_deltas), bool(self._props)
        _log.info("query.start query={} fields={}".format(self._filter, fields))
        t0 = time.time()

        # Main query loop.
        for i, coll in enumerate(engines):
            _log.debug("collection {:d}".format(i))
            count, missing_props = 0, 0
            for rec in coll.query(criteria=self._filter, properties=fields):
                count += 1
                # Extract key from record.
                try:
                    key = rec[self._key_field]
                except KeyError:
                    _log.critical("Key '{}' not found in record: {}. Abort.".format(
                        self._key_field, rec))
                    return {}
                if not allow_dup and key in keys[i]:
                    raise ValueError("Duplicate key: {}".format(key))
                keys[i].add(key)
                # Extract numeric properties.
                if has_numprops:
                    pvals = {}
                    for pkey in self._prop_deltas.iterkeys():
                        try:
                            pvals[pkey] = float(rec[pkey])
                        except KeyError:
                            #print("@@ missing {} on {}".format(pkey, rec))
                            missing_props += 1
                            continue
                        except (TypeError, ValueError):
                            raise ValueError("Not a number: collection={c} key={k} {p}='{v}'"
                                             .format(k=key, c=("old", "new")[i], p=pkey, v=rec[pkey]))
                    numprops[i][key] = pvals
                # Extract properties for exact match.
                if has_eqprops:
                    try:
                        propval = tuple([(p, str(rec[p])) for p in self._props])
                    except KeyError:
                        missing_props += 1
                        #print("@@ missing {} on {}".format(pkey, rec))
                        continue
                    eqprops[i][key] = propval

                # Extract informational fields.
                if has_info:
		    if key not in info:
		        info[key] = {}
                    for k in self._info:
                        info[key][k] = rec[k]

            # Stop if we don't have properties on any record at all
            if 0 < count == missing_props:
                _log.critical("Missing one or more properties on all {:d} records"
                              .format(count))
                return {}
            # ..but only issue a warning for partially missing properties.
            elif missing_props > 0:
                _log.warn("Missing one or more properties for {:d}/{:d} records"
                          .format(missing_props, count))
        t1 = time.time()
        _log.info("query.end sec={:f}".format(t1 - t0))

        # Compute missing and new keys.
        _log.debug("compute_difference.start")
        missing = keys[0] - keys[1]
        if not only_missing:
            new = keys[1] - keys[0]
        _log.debug("compute_difference.end")

        # Compute mis-matched properties.
        if has_props:
            changed = self._changed_props(keys, eqprops, numprops, info,
                                          has_eqprops=has_eqprops, has_numprops=has_numprops)
        else:
            changed = []

        # Build result.
        _log.debug("build_result.begin")
        result = {}
        result[self.MISSING] = []
        for key in missing:
	    rec = {self._key_field: key}
            if has_info:
                rec.update(info.get(key, {}))
	    result[self.MISSING].append(rec)
        if not only_missing:
            result[self.NEW] = []
            for key in new:
	        rec = {self._key_field: key}
                if has_info:
		    rec.update(info.get(key,{}))
                result[self.NEW].append(rec)
        result[self.CHANGED] = changed
        _log.debug("build_result.end")

        return result

    def _changed_props(self, keys=None, eqprops=None, numprops=None, info=None,
                       has_numprops=False, has_eqprops=False):
        changed = []
        _up = lambda d, v: d.update(v) or d   # functional dict.update()
        for key in keys[0].intersection(keys[1]):
            # Numeric property comparisons.
            if has_numprops:
                for pkey in self._prop_deltas:
                    oldval, newval = numprops[0][key][pkey], numprops[1][key][pkey]
                    if self._prop_deltas[pkey].cmp(oldval, newval):
                        change = {"match_type": "delta", self._key_field: key, "property": pkey,
                                  "old": "{:f}".format(oldval), "new": "{:f}".format(newval),
                                  "rule": self._prop_deltas[pkey]}
                        changed.append(_up(change, info[key]) if info else change)
            # Exact property comparison.
            if has_eqprops:
                if not eqprops[0][key] == eqprops[1][key]:
                    change = {"match_type": "exact", self._key_field: key,
                              "old": eqprops[0][key], "new": eqprops[1][key]}
                    changed.append(_up(change, info[key]) if info else change)
        return changed


class Delta(object):
    """Delta between two properties.

    Syntax:
        +-       Change in sign
        +-X      abs(new - old) > X
        +X-Y     (new - old) > X or (old - new) > Y
        +-X=     abs(new - old) >= X
        +X-Y=    (new - old) >= X or (old - new) >= Y
        ...%     Instead of (v2 - v1), use 100*(v2 - v1)/v1
    """
    _num = "\d+(\.\d+)?"
    _expr = re.compile("\+(?P<X>{n})?-(?P<Y>{n})?(?P<eq>=)?(?P<pct>%)?".format(n=_num))

    def __init__(self, s):
        """Constructor.

        :param s: Expression string
        :type s: str
        :raises: ValueError if it doesn't match the syntax
        """
        # Match expression.
        m = self._expr.match(s)
        if m is None:
            raise ValueError("Bad syntax for delta '{}'".format(s))
        if m.span()[1] != len(s):
            p = m.span()[1]
            raise ValueError("Junk at end of delta '{}': {}".format(s, s[p:]))


        # Save a copy of orig.
        self._orig_expr = s

        # Initialize parsed values.
        self._sign = False
        self._dx, self._dy = 0, 0
        self._pct = False           # %change
        self._eq = False            # >=,<= instead of >, <

        # Set parsed values.
        d = m.groupdict()
        #print("@@ expr :: {}".format(d))
        if d['X'] is None and d['Y'] is None:
            # Change in sign only
            self._sign = True
        elif d['X'] is not None and d['Y'] is None:
            raise ValueError("Bad syntax for delta '{}': +X-".format(s))
        else:
            # Main branch for +-XY
            self._dy = -float(d['Y'])
            self._dx = float(d['X'] or d['Y'])
            self._eq = d['eq'] is not None
            self._pct = d['pct'] is not None
            #print("@@ dx,dy eq,pct = {},{}  {},{}".format(self._dx, self._dy, self._eq, self._pct))

        # Pre-calculate comparison function.
        if self._sign:
            self._cmp = self._cmp_sign
        elif self._pct:
            self._cmp = self._cmp_val_pct
        else:
            self._cmp = self._cmp_val

    def __str__(self):
        return self._orig_expr

    def cmp(self, old, new):
        """Compare numeric values with delta expression.

        Returns True if delta matches (is as large or larger than) this class' expression.

        Delta is computed as (new - old).

        :param old: Old value
        :type old: float
        :param new: New value
        :type new: float
        :return: True if delta between old and new is large enough, False otherwise
        :rtype: bool
        """
        return self._cmp(old, new)

    def _cmp_sign(self, a, b):
        return (a < 0 < b) or (a > 0 > b)

    def _cmp_val(self, a, b):
        delta = b - a
        #print("@@ val cmp {:f}".format(delta))
        if self._eq:
            return delta >= self._dx or delta <= self._dy
        return delta > self._dx or delta < self._dy

    def _cmp_val_pct(self, a, b):
        if a == 0:
            return False
        delta = 100.0 * (b - a) / a
        if self._eq:
            return delta >= self._dx or delta <= self._dy
        return delta > self._dx or delta < self._dy
