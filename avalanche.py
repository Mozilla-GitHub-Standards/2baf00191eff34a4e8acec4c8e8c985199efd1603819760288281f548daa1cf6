################################################################################
# coding=utf-8
# pylint: disable=missing-docstring,too-many-lines
#
# Description: Grammar based generation/fuzzer
#
# Portions Copyright 2014 BlackBerry Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
################################################################################

from __future__ import unicode_literals
import argparse
import binascii
import hashlib
import io
import logging as log
import numbers
import os
import os.path
import random
import re
import sys


__all__ = ("Grammar", "GrammarException", "ParseError", "IntegrityError", "GenerationError",
           "BinSymbol", "ChoiceSymbol", "ConcatSymbol", "FuncSymbol", "RefSymbol", "RepeatSymbol",
           "RepeatSampleSymbol", "RegexSymbol", "TextSymbol")


if sys.version_info.major == 2:
    # pylint: disable=redefined-builtin,invalid-name
    str = unicode


DEFAULT_LIMIT = 100 * 1024

if bool(os.getenv("DEBUG")):
    log.getLogger().setLevel(log.DEBUG)


class GrammarException(Exception):
    def __str__(self):
        if len(self.args) == 2:
            msg, state = self.args
            if isinstance(state, _ParseState):
                return "%s (%sline %d)" % (msg, "%s " % state.name if state.name else "", state.line_no)
            if isinstance(state, _GenState):
                return "%s (generation backtrace: %s)" % (msg, state.backtrace())
            return "%s (line %d)" % (msg, state) # state is line_no in this case
        if len(self.args) == 1:
            return str(self.args[0])
        return str(self.args)
class ParseError(GrammarException):
    pass
class IntegrityError(GrammarException):
    pass
class GenerationError(GrammarException):
    pass


class _GenState(object):

    def __init__(self, grmr):
        self.symstack = []
        self.instances = {}
        self.output = []
        self.grmr = grmr
        self.length = 0

    def append(self, value):
        if self.output and not isinstance(value, type(self.output[0])):
            raise GenerationError("Wrong value type generated, expecting %s, got %s" % (type(self.output[0]).__name__,
                                                                                        type(value).__name__), self)
        self.output.append(value)
        self.length += len(value)

    def backtrace(self):
        return ", ".join(sym[1] for sym in self.symstack
                         if isinstance(sym, tuple) and sym[0] == 'unwind')


class _ParseState(object):

    def __init__(self, prefix, grmr, filename):
        self.prefix = prefix
        self.imports = {} # friendly name -> (grammar hash, import line_no)
        self.imports_used = set() # friendly names used by get_prefixed()
        self.line_no = 0
        self.n_implicit = -1
        self.grmr = grmr
        self.name = filename

    def implicit(self):
        self.n_implicit += 1
        return self.n_implicit

    def get_prefixed(self, symprefix, sym):
        if symprefix:
            symprefix = symprefix[:-1]
            try:
                newprefix = self.imports[symprefix][0]
                self.imports_used.add(symprefix)
                symprefix = newprefix
            except KeyError:
                raise ParseError("Attempt to use symbol from unknown prefix: %s" % symprefix, self)
        else:
            symprefix = self.prefix
        return "%s.%s" % (symprefix, sym)

    def add_import(self, name, grammar_hash):
        self.imports[name] = (grammar_hash, self.line_no)

    def sanity_check(self):
        unused = set(self.imports) - self.imports_used
        if unused:
            raise IntegrityError("Unused import%s: %s" % ("s" if len(unused) > 1 else "", list(unused)), self)


class _WeightedChoice(object):

    def __init__(self, iterable=None):
        self.total = 0.0
        self.values = []
        self.weights = []
        if iterable is not None:
            self.extend(iterable)

    def extend(self, iterable):
        for value in iterable:
            self.append(*value)

    def append(self, value, weight):
        if weight != '+':
            self.total += weight
        self.values.append(value)
        self.weights.append(weight)

    def choice(self):
        target = random.uniform(0, self.total)
        for weight, value in zip(self.weights, self.values):
            target -= weight
            if target < 0:
                return value
        raise AssertionError("Too much total weight? remainder is %0.2f from %0.2f total" % (target, self.total))

    def sample(self, k):
        weights, values, total = self.weights[:], self.values[:], self.total
        result = []
        while k and total:
            target = random.uniform(0, total)
            for i, (weight, value) in enumerate(zip(weights, values)):
                target -= weight
                if target < 0:
                    result.append(value)
                    total -= weight
                    k -= 1
                    del weights[i]
                    del values[i]
                    break
            else:
                raise AssertionError("Too much total weight? remainder is %0.2f from %0.2f total" % (target, total))
        return result

    def __repr__(self):
        return "WeightedChoice(%s)" % list(zip(self.values, self.weights))


class Grammar(object):
    """Generate a language conforming to a given grammar specification.

       A Grammar consists of a set of symbol definitions which are used to define the structure of a language. The Grammar
       object is created from a text input with the format described below, and then used to generate randomly constructed
       instances of the described language. The entrypoint of the grammar is the named symbol 'root'. Comments are allowed
       anywhere in the file, preceded by a hash character (``#``).

       Symbols can either be named or implicit. A named symbol consists of a symbol name at the beginning of a line,
       followed by at least one whitespace character, followed by the symbol definition.

       ::

           SymbolName  Definition

       Implicit symbols are defined without being assigned an explicit name. For example a regular expression can be used
       in a concatenation definition directly, without being assigned a name. Choice symbols cannot be defined implicitly.

       ::

           ModuleName  import("filename")

       Imports allow you to break up grammars into multiple files. A grammar which imports another assigns it a local
       name ``ModuleName``, which may be used to access symbols from that grammar such as ``ModuleName.Symbol``, etc.
       Everything should work as expected, including references. Modules must be imported before they can be used.
    """
    _RE_LINE = re.compile(r"""^((?P<broken>.*)\\
                                |\s*(?P<comment>\#).*
                                |(?P<nothing>\s*)
                                |(?P<name>[\w:-]+)
                                 (?P<type>((?P<weight>\s+\d+\s+)
                                           |\s*\+\s*
                                           |\s+import\(\s*)
                                  |\s+)
                                 (?P<def>.+)
                                |\s+(\+|(?P<contweight>\d+))\s*(?P<cont>.+))$
                           """, re.VERBOSE)

    def __init__(self, grammar="", limit=DEFAULT_LIMIT, **kwargs):
        self._limit = limit
        self.symtab = {}
        self.tracked = set()
        self.funcs = kwargs
        if "rndint" not in self.funcs:
            self.funcs["rndint"] = lambda a, b: str(random.randint(int(a), int(b)))
        if "rndpow2" not in self.funcs:
            self.funcs["rndpow2"] = lambda a, b: str(2 ** random.randint(0, int(a)) + random.randint(-int(b), int(b)))
        if "rndflt" not in self.funcs:
            self.funcs["rndflt"] = lambda a, b: str(random.uniform(float(a), float(b)))
        if "import" in self.funcs:
            raise IntegrityError("'import' is a reserved function name")

        need_to_close = False
        if hasattr(grammar, "read"):
            if isinstance(grammar.read(1), bytes):
                # need to reopen as unicode
                grammar.seek(0)
                try:
                    grammar = open(grammar.name, 'r') # will fail if grammar is not a named file...
                    need_to_close = True
                except (AttributeError, IOError):
                    # can't reopen, no choice but to read the whole input
                    grammar = io.StringIO(grammar.read().decode("utf-8"))
        elif isinstance(grammar, bytes):
            grammar = io.StringIO(grammar.decode("utf-8"))
        else:
            grammar = io.StringIO(grammar)

        # Initial definitions use hash of the grammar as the prefix, keeping track of the first used friendly name
        # ("" for top level). When grammar and imports are fully parsed, do a final pass to rename hash prefixes to
        # friendly prefixes.

        imports = {} # hash -> friendly prefix
        try:
            self.parse(grammar, imports)
        finally:
            if need_to_close:
                grammar.close()
        self.normalize(imports)
        self.sanity_check()

    def parse(self, grammar, imports, prefix=""):
        grammar_hash = hashlib.sha512()
        while True:
            hash_str = grammar.read(4096)
            grammar_hash.update(hash_str.encode("utf-8"))
            if len(hash_str) < 4096:
                break
        grammar_hash = grammar_hash.hexdigest()[:6]
        grammar_fn = getattr(grammar, "name", None)
        if grammar_hash in imports:
            return grammar_hash
        imports[grammar_hash] = prefix
        grammar.seek(0)
        pstate = _ParseState(grammar_hash, self, grammar_fn)

        sym = None
        ljoin = ""
        for line in grammar:
            pstate.line_no += 1
            pstate.n_implicit = -1
            log.debug("parsing line # %d: %s", pstate.line_no, line.rstrip())
            match = Grammar._RE_LINE.match("%s%s" % (ljoin, line))
            if match is None:
                raise ParseError("Failed to parse definition at: %s%s" % (ljoin, line.rstrip()), pstate)
            if match.group("broken") is not None:
                ljoin = match.group("broken")
                continue
            ljoin = ""
            if match.group("comment") or match.group("nothing") is not None:
                continue
            if match.group("name"):
                sym_name, sym_type, sym_def = match.group("name", "type", "def")
                sym_type = sym_type.lstrip()
                if sym_type.startswith("+") or match.group("weight"):
                    # choice
                    weight = float(match.group("weight")) if match.group("weight") else "+"
                    sym = ChoiceSymbol(sym_name, pstate)
                    sym.append(_Symbol.parse(sym_def, pstate), weight)
                elif sym_type.startswith("import("):
                    # import
                    if "%s.%s" % (grammar_hash, sym_name) in self.symtab:
                        raise ParseError("Redefinition of symbol %s previously declared on line %d"
                                         % (sym_name, self.symtab["%s.%s" % (grammar_hash, sym_name)].line_no), pstate)
                    sym, defn = TextSymbol.parse(sym_def, pstate, no_add=True)
                    defn = defn.strip()
                    if not defn.startswith(")"):
                        raise ParseError("Expected ')' parsing import at: %s" % defn, pstate)
                    defn = defn[1:].lstrip()
                    if defn.startswith("#") or defn:
                        raise ParseError("Unexpected input following import: %s" % defn, pstate)
                    # resolve sym.value from current grammar path or "."
                    import_paths = [sym.value]
                    if grammar_fn is not None:
                        import_paths.insert(0, os.path.join(os.path.dirname(grammar_fn), sym.value))
                    for import_fn in import_paths:
                        try:
                            with open(import_fn) as import_fd:
                                pstate.add_import(sym_name, self.parse(import_fd, imports, prefix=sym_name))
                            break
                        except IOError:
                            pass
                    else:
                        raise IntegrityError("Could not find imported grammar: %s" % sym.value, pstate)
                else:
                    # sym def
                    sym = ConcatSymbol.parse(sym_name, sym_def, pstate)
            else:
                # continuation of choice
                if sym is None or not isinstance(sym, ChoiceSymbol):
                    raise ParseError("Unexpected continuation of choice symbol", pstate)
                weight = float(match.group("contweight")) if match.group("contweight") else "+"
                sym.append(_Symbol.parse(match.group("cont"), pstate), weight)

        pstate.sanity_check()
        return grammar_hash

    def normalize(self, imports):
        def get_prefixed(symname):
            try:
                prefix, name = symname.split(".", 1)
            except ValueError:
                return symname
            ref = prefix.startswith("@")
            if ref:
                prefix = prefix[1:]
            try:
                newprefix = imports[prefix]
            except KeyError:
                raise ParseError("Failed to reassign %s to proper namespace after parsing" % symname)
            newname = "".join((newprefix, "." if newprefix else "", name))
            if symname != newname:
                log.debug('reprefixed %s -> %s', symname, newname)
            return "".join(("@" if ref else "", newname))

        # rename prefixes to friendly names
        for oldname in list(self.symtab):
            sym = self.symtab[oldname]
            assert oldname == sym.name
            newname = get_prefixed(oldname)
            if oldname != newname:
                sym.name = newname
                self.symtab[newname] = sym
                del self.symtab[oldname]
            sym.map(get_prefixed)
        self.tracked = {get_prefixed(t) for t in self.tracked}

        # normalize symbol tree (remove implicit concats, etc.)
        while True:
            for name in list(self.symtab):
                try:
                    sym = self.symtab[name]
                except KeyError:
                    continue # can happen if symbol is optimized out
                sym.normalize(self)
            else:
                break

    def sanity_check(self):
        log.debug("sanity checking symtab: %s", self.symtab)
        funcs_used = {"rndflt", "rndint", "rndpow2"}
        for sym in self.symtab.values():
            sym.sanity_check(self)
            if isinstance(sym, FuncSymbol):
                funcs_used.add(sym.fname)
        if set(self.funcs) != funcs_used:
            unused_kwds = tuple(set(self.funcs) - funcs_used)
            raise IntegrityError("Unused keyword argument%s: %s" % ("s" if len(unused_kwds) > 1 else "", unused_kwds))
        if "root" not in self.symtab:
            raise IntegrityError("Missing required start symbol: root")
        syms_used = {"root"}
        to_check = {"root"}
        checked = set()
        while to_check:
            sym = self.symtab[to_check.pop()]
            checked.add(sym.name)
            children = sym.children()
            log.debug("%s is %s with %d children %s", sym.name, type(sym).__name__, len(children), list(children))
            syms_used |= children
            to_check |= children - checked
        # ignore unused symbols that came from an import, Text, Regex, or Bin
        syms_ignored = {s for s in self.symtab if re.search(r"[.\[]", s)}
        unused_syms = list(set(self.symtab) - syms_used - syms_ignored)
        if unused_syms:
            raise IntegrityError("Unused symbol%s: %s" % ("s" if len(unused_syms) > 1 else "", unused_syms))
        # build paths to terminal symbols
        do_over = True
        while do_over:
            do_over = False
            for sym in self.symtab.values():
                if sym.can_terminate is None:
                    do_over = sym.update_can_terminate(self) or do_over
        for sym in self.symtab.values():
            if not (sym.can_terminate or any(self.symtab[child].can_terminate for child in sym.children())):
                raise IntegrityError("Symbol has no paths to termination (infinite recursion?): %s" % sym.name,
                                     sym.line_no)

    def is_limit_exceeded(self, length):
        return self._limit is not None and length >= self._limit

    def generate(self, start="root"):
        if not isinstance(start, _GenState):
            gstate = _GenState(self)
            gstate.symstack = [start]
            gstate.instances = {sym: [] for sym in self.tracked}
        else:
            gstate = start
        tracking = []
        while gstate.symstack:
            this = gstate.symstack.pop()
            if isinstance(this, tuple):
                if this[0] == 'unwind':
                    continue
                assert this[0] == "untrack", "Tracking mismatch: expected ('untrack', ...), got %r" % this
                tracked = tracking.pop()
                assert this[1] == tracked[0], "Tracking mismatch: expected '%s', got '%s'" % (tracked[0], this[1])
                instance = "".join(gstate.output[tracked[1]:])
                gstate.instances[this[1]].append(instance)
                continue
            if this in self.tracked: # need to capture everything generated by this symbol and add to "instances"
                gstate.symstack.append(("untrack", this))
                tracking.append((this, len(gstate.output)))
            gstate.symstack.append(('unwind', this))
            self.symtab[this].generate(gstate)
        try:
            return "".join(gstate.output)
        except TypeError:
            return b"".join(gstate.output)


class _Symbol(object):
    _RE_DEFN = re.compile(r"""^((?P<quote>["'])
                                |(?P<hexstr>x["'])
                                |(?P<regex>/)
                                |(?P<implconcat>\()
                                |(?P<infunc>[,)])
                                |(?P<comment>\#).*
                                |(?P<func>\w+)\(
                                |(?P<maybe>\?)
                                |(?P<repeat>[{<]\s*(?P<a>\d+|\*)\s*(,\s*(?P<b>\d+|\*)\s*)?[}>])
                                |@(?P<refprefix>[\w-]+\.)?(?P<ref>[\w:-]+)
                                |(?P<symprefix>[\w-]+\.)?(?P<sym>[\w:-]+)
                                |(?P<ws>\s+))""", re.VERBOSE)

    def __init__(self, name, pstate, no_add=False):
        if name == '%s.import' % pstate.prefix:
            raise ParseError("'import' is a reserved name", pstate)
        unprefixed = name.split(".", 1)[1]
        if unprefixed in pstate.imports:
            raise ParseError("Redefinition of symbol %s previously declared on line %d"
                             % (unprefixed, pstate.imports[unprefixed][1]), pstate)
        self.name = name
        self.line_no = pstate.line_no
        log.debug('\t%s %s', type(self).__name__.lower()[:-6], name)
        if not no_add:
            if name in pstate.grmr.symtab and not isinstance(pstate.grmr.symtab[name], (_AbstractSymbol, RefSymbol)):
                unprefixed = name.split(".", 1)[1]
                raise ParseError("Redefinition of symbol %s previously declared on line %d"
                                 % (unprefixed, pstate.grmr.symtab[name].line_no), pstate)
            pstate.grmr.symtab[name] = self
        self.can_terminate = None

    def map(self, fcn):
        pass

    def normalize(self, grmr):
        pass

    def sanity_check(self, grmr):
        pass

    def generate(self, gstate):
        raise GenerationError("Can't generate symbol %s of type %s" % (self.name, type(self)), gstate)

    def children(self):
        return set()

    def update_can_terminate(self, grmr):
        if all(grmr.symtab[c].can_terminate for c in self.children()):
            self.can_terminate = True
            return True
        return False

    @staticmethod
    def _parse(defn, pstate, in_func, in_concat):
        result = []
        while defn:
            match = _Symbol._RE_DEFN.match(defn)
            if match is None:
                raise ParseError("Failed to parse definition at: %s" % defn, pstate)
            log.debug("parsed %s from %s", {k: v for k, v in match.groupdict().items() if v is not None}, defn)
            if match.group("ws") is not None:
                defn = defn[match.end(0):]
                continue
            if match.group("quote"):
                sym, defn = TextSymbol.parse(defn, pstate)
            elif match.group("hexstr"):
                sym, defn = BinSymbol.parse(defn, pstate)
            elif match.group("regex"):
                sym, defn = RegexSymbol.parse(defn, pstate)
            elif match.group("func"):
                defn = defn[match.end(0):]
                sym, defn = FuncSymbol.parse(match.group("func"), defn, pstate)
            elif match.group("ref"):
                ref = pstate.get_prefixed(match.group("refprefix"), match.group("ref"))
                sym = RefSymbol(ref, pstate)
                defn = defn[match.end(0):]
            elif match.group("sym"):
                sym_name = pstate.get_prefixed(match.group("symprefix"), match.group("sym"))
                try:
                    sym = pstate.grmr.symtab[sym_name]
                except KeyError:
                    sym = _AbstractSymbol(sym_name, pstate)
                defn = defn[match.end(0):]
            elif match.group("comment"):
                defn = ""
                break
            elif match.group("infunc"):
                if in_func or (in_concat and match.group("infunc") == ")"):
                    break
                raise ParseError("Unexpected token in definition: %s" % defn, pstate)
            elif match.group("implconcat"):
                parts, defn = _Symbol._parse(defn[match.end(0):], pstate, False, True)
                if not defn.startswith(")"):
                    raise ParseError("Expecting ) at: %s" % defn, pstate)
                name = "[concat (line %d #%d)]" % (pstate.line_no, pstate.implicit())
                sym = ConcatSymbol(name, pstate)
                sym.extend(parts)
                defn = defn[1:]
            elif match.group("maybe") or match.group("repeat"):
                if not result:
                    raise ParseError("Unexpected token in definition: %s" % defn, pstate)
                if match.group("maybe"):
                    repeat = RepeatSymbol
                    min_, max_ = 0, 1
                else:
                    if {"{": "}", "<": ">"}[match.group(0)[0]] != match.group(0)[-1]:
                        raise ParseError("Repeat symbol mismatch at: %s" % defn, pstate)
                    repeat = {"{": RepeatSymbol, "<": RepeatSampleSymbol}[match.group(0)[0]]
                    min_ = "*" if match.group("a") == "*" else int(match.group("a"))
                    max_ = ("*" if match.group("b") == "*" else int(match.group("b"))) if match.group("b") else min_
                parts = result.pop()
                name = "[repeat (line %d #%d)]" % (pstate.line_no, pstate.implicit())
                sym = repeat(name, min_, max_, pstate)
                if "[concat" in pstate.grmr.symtab[parts].name:
                    # use the children directly and remove the intermediate concat
                    sym.extend(pstate.grmr.symtab[parts])
                    del pstate.grmr.symtab[parts]
                else:
                    sym.append(parts)
                defn = defn[match.end(0):]
            result.append(sym.name)
        return result, defn

    @staticmethod
    def parse_func_arg(defn, pstate):
        return _Symbol._parse(defn, pstate, True, False)

    @staticmethod
    def parse(defn, pstate):
        res, remain = _Symbol._parse(defn, pstate, False, False)
        if remain:
            raise ParseError("Unexpected token in definition: %s" % remain, pstate)
        return res


class _AbstractSymbol(_Symbol):

    def __init__(self, name, pstate):
        _Symbol.__init__(self, name, pstate)

    def sanity_check(self, grmr):
        raise IntegrityError("Symbol %s used but not defined" % self.name, self.line_no)


class BinSymbol(_Symbol):
    """Binary data

       ::

           SymbolName      x'41414141'

       Defines a chunk of binary data encoded in hex notation. BinSymbol and TextSymbol cannot be combined in the
       output.
    """

    _RE_QUOTE = re.compile(r"""(?P<end>["'])""")

    def __init__(self, value, pstate):
        name = "%s.[bin (line %d #%d)]" % (pstate.prefix, pstate.line_no, pstate.implicit())
        _Symbol.__init__(self, name, pstate)
        try:
            self.value = binascii.unhexlify(value.encode("ascii"))
        except (UnicodeEncodeError, TypeError) as err:
            raise ParseError("Invalid hex string: %s" % err, pstate)
        self.can_terminate = True

    def generate(self, gstate):
        gstate.append(self.value)

    @staticmethod
    def parse(defn, pstate):
        start, qchar, defn = defn[0], defn[1], defn[2:]
        if start != "x":
            raise ParseError("Error parsing binary string at: %s%s%s" % (start, qchar, defn), pstate)
        if qchar not in "'\"":
            raise ParseError("Error parsing binary string at: %s%s" % (qchar, defn), pstate)
        enquo = defn.find(qchar)
        if enquo == -1:
            raise ParseError("Unterminated bin literal!", pstate)
        value, defn = defn[:enquo], defn[enquo+1:]
        sym = BinSymbol(value, pstate)
        return sym, defn


class ChoiceSymbol(_Symbol, _WeightedChoice):
    """Choose between several options

       ::

           SymbolName      Weight1     SubSymbol1
                          [Weight2     SubSymbol2]
                          [Weight3     SubSymbol3]

       A choice consists of one or more weighted sub-symbols. At generation, only one of the sub-symbols will be
       generated at random, with each sub-symbol being generated with probability of weight/sum(weights) (the sum of
       all weights in this choice). Weight can be a non-negative integer.

       Weight can also be ``+``, which imports another ChoiceSymbol into this definition. SubSymbol must be another
       ChoiceSymbol, and the total weight of that symbol will be used as the weight in this choice definition.
    """

    def __init__(self, name, pstate=None, _test=False):
        if not _test:
            name = "%s.%s" % (pstate.prefix, name)
            _Symbol.__init__(self, name, pstate)
        _WeightedChoice.__init__(self)
        self._choices_terminate = []
        if _test:
            self.extend(name)

    def append(self, value, weight):
        _WeightedChoice.append(self, value, weight)
        self._choices_terminate.append(None)

    def normalize(self, grmr):
        for i, (value, weight) in enumerate(zip(self.values, self.weights)):
            if weight == '+':
                if len(value) == 1 and isinstance(grmr.symtab[value[0]], ChoiceSymbol):
                    if any(weight == '+' for weight in grmr.symtab[value[0]].weights):
                        grmr.symtab[value[0]].normalize(grmr) # resolve the child '+' first, could recurse forever :(
                    self.weights[i] = grmr.symtab[value[0]].total
                else:
                    raise IntegrityError("Invalid use of '+' on non-ChoiceSymbol in %s" % self.name, self.line_no)
                self.total += self.weights[i]

    def generate(self, gstate):
        try:
            if gstate.grmr.is_limit_exceeded(gstate.length) and self.can_terminate:
                terminators = _WeightedChoice()
                for i in range(len(self.values)):
                    if self._choices_terminate[i]:
                        terminators.append(self.values[i], self.weights[i])
                gstate.symstack.extend(reversed(terminators.choice()))
            else:
                gstate.symstack.extend(reversed(self.choice()))
        except AssertionError as err:
            raise GenerationError(err, gstate)

    def children(self):
        children = set()
        for child in self.values:
            children |= set(child)
        return children

    def map(self, fcn):
        self.values = [[fcn(i) for i in j] for j in self.values]

    def update_can_terminate(self, grmr):
        for i, choice in enumerate(self.values):
            if all(grmr.symtab[child].can_terminate for child in choice):
                self._choices_terminate[i] = True
        if any(self._choices_terminate):
            self.can_terminate = True
            return True
        return False


class ConcatSymbol(_Symbol, list):
    """Concatenation of subsymbols

       ::

           SymbolName      SubSymbol1 [SubSymbol2] ...

       A concatenation consists of one or more symbols which will be generated in succession. The sub-symbol can be
       any named symbol, reference, or an implicit declaration of terminal symbol types. A concatenation can also be
       implicitly used as the sub-symbol of a choice or repeat symbol, or inline using ``(`` and ``)``. eg::

           SymbolName      SubSymbol1 ( SubSymbol2 SubSymbol3 ) ...

       This is most useful for defining implicit repeats for some terms in the concatenation.
    """

    def __init__(self, name, pstate, no_prefix=False):
        name = "%s.%s" % (pstate.prefix, name) if not no_prefix else name
        _Symbol.__init__(self, name, pstate)
        list.__init__(self)

    def normalize(self, grmr):
        # if I only have one implicit child, there's no reason for me to exist
        if len(self) == 1 and "[" in self[0]: # all implicit symbols are named like "[blah #0]"
            # give child my name
            child_name = self[0]
            child = grmr.symtab[child_name]
            log.debug("concat has only one implicit child, renaming %s to %s (line %d)",
                      child_name, self.name, self.line_no)
            child.name = self.name
            child.line_no = self.line_no # could be different if line was broken?
            grmr.symtab[self.name] = child
            del grmr.symtab[child_name]
            # boom, I don't exist
            child.normalize(grmr) # may not get called otherwise

    def children(self):
        return set(self)

    def map(self, fcn):
        list.__init__(self, [fcn(i) for i in self])

    def generate(self, gstate):
        gstate.symstack.extend(reversed(self))

    @staticmethod
    def parse(name, defn, pstate):
        result = ConcatSymbol(name, pstate)
        result.extend(_Symbol.parse(defn, pstate))
        return result


class FuncSymbol(_Symbol):
    """Function

       ::

           SymbolName      function(SymbolArg1[,...])

       This denotes an externally defined function. The function name can be any valid Python identifier. It can
       accept an arbitrary number of arguments, but must return a single string which is the generated value for
       this symbol instance. Functions must be passed as keyword arguments into the Grammar object constructor.

       The following functions are built-in::

           rndflt(a,b)      A random floating-point decimal number between ``a`` and ``b`` inclusive.
           rndint(a,b)      A random integer between ``a`` and ``b`` inclusive.
           rndpow2(exponent_limit, variation)
                            This function is intended to return edge values around powers of 2. It is equivalent to:
                            ``pow(2, rndint(0, exponent_limit)) + rndint(-variation, variation)``
    """

    def __init__(self, name, pstate):
        sname = "%s.[%s (line %d #%d)]" % (pstate.prefix, name, pstate.line_no, pstate.implicit())
        _Symbol.__init__(self, sname, pstate)
        self.fname = name
        self.args = []

    def sanity_check(self, grmr):
        if self.fname not in grmr.funcs:
            raise IntegrityError("Function %s used but not defined" % self.fname, self.line_no)

    def generate(self, gstate):
        args = []
        for arg in self.args:
            if isinstance(arg, numbers.Number):
                args.append(arg)
            else:
                astate = _GenState(gstate.grmr)
                astate.symstack = [arg]
                astate.instances = gstate.instances
                args.append(gstate.grmr.generate(astate))
        gstate.append(gstate.grmr.funcs[self.fname](*args))

    def children(self):
        return set(a for a in self.args if not isinstance(a, numbers.Number))

    def map(self, fcn):
        _fcn = lambda x: x if isinstance(x, numbers.Number) else fcn(x)
        self.args = [_fcn(i) for i in self.args]

    @staticmethod
    def parse(name, defn, pstate):
        if name == "import":
            raise ParseError("'import' is a reserved function name", pstate)
        result = FuncSymbol(name, pstate)
        done = False
        while not done:
            arg, defn = _Symbol.parse_func_arg(defn, pstate)
            if defn[0] not in ",)":
                raise ParseError("Expected , or ) parsing function args at: %s" % defn, pstate)
            done = defn[0] == ")"
            defn = defn[1:]
            if arg or not done:
                numeric_arg = False
                if len(arg) == 1 and isinstance(pstate.grmr.symtab[arg[0]], _AbstractSymbol):
                    arg0 = arg[0].split(".", 1)[1]
                    for numtype in (int, float):
                        try:
                            value = numtype(arg0)
                            result.args.append(value)
                            del pstate.grmr.symtab[arg[0]]
                            numeric_arg = True
                            break
                        except ValueError:
                            pass
                if not numeric_arg:
                    sym = ConcatSymbol("%s.%s]" % (result.name[:-1], len(result.args)), pstate, no_prefix=True)
                    sym.extend(arg)
                    result.args.append(sym.name)
        return result, defn


class RefSymbol(_Symbol):
    """Reference an instance of another symbol

       ::

           SymbolRef       @SymbolName

       Symbol references allow a generated symbol to be used elsewhere in the grammar. Referencing a symbol by
       ``@Symbol`` will output a generated value of ``Symbol`` from elsewhere in the output.
   """

    def __init__(self, ref, pstate):
        _Symbol.__init__(self, "@%s" % ref, pstate)
        if ref not in pstate.grmr.symtab:
            pstate.grmr.symtab[ref] = _AbstractSymbol(ref, pstate)
        self.ref = ref
        pstate.grmr.tracked.add(ref)

    def generate(self, gstate):
        if gstate.instances[self.ref]:
            gstate.append(random.choice(gstate.instances[self.ref]))
        else:
            log.debug("No instances of %s yet, generating one instead of a reference", self.ref)
            gstate.grmr.symtab[self.ref].generate(gstate)

    def children(self):
        return {self.ref}

    def map(self, fcn):
        self.ref = fcn(self.ref)


class RegexSymbol(ConcatSymbol):
    """Text generated by a regular expression

       ::

           SymbolName      /[a-zA][0-9]*.+[^0-9]{2}.[^abc]{1,3}/
           ...             /a?far/  (generates either 'far' or 'afar')

       A regular expression (regex) symbol is a minimal regular expression implementation used for generating text
       patterns (rather than the traditional use for matching text patterns). A regex symbol consists of one or more
       parts in succession, and each part consists of a character set definition optionally followed by a repetition
       specification.

       The character set definition can be a single character, a period ``.`` to denote any ASCII character, a set of
       characters in brackets eg. ``[0-9a-f]``, or an inverted set of characters ``[^a-z]`` (any character except
       a-z). As shown, ranges can be defined by using a dash. The dash character can be matched in a set by putting it
       first or last in the set. Escapes work as in TextSymbol using the backslash character.

       The optional repetition specification can be a range of integers in curly braces, eg. ``{1,10}`` will generate
       between 1 and 10 repetitions (at random), a single integer in curly braces, eg. ``{10}`` will generate exactly
       10 repetitions, an asterisk character (``*``) which is equivalent to ``{0,5}``, a plus character (``+``) which
       is equivalent to ``{1,5}``, or a question mark (``?``) which is equivalent to ``{0,1}``.

       A notable exclusion from ordinary regular expression implementations is groups using ``()`` or ``(a|b)``. This
       syntax is *not* supported in RegexSymbol. The characters "()|" have no special meaning and do not need to be
       escaped.
    """
    _REGEX_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" \
                      "abcdefghijklmnopqrstuvwxyz" \
                      "0123456789" \
                      ",./<>?;':\"[]\\{}|=_+`~!@#$%^&*() -"
    _RE_PARSE = re.compile(r"""^((?P<repeat>\{\s*(?P<a>\d+)\s*(,\s*(?P<b>\d+)\s*)?\}|\?)
                                 |(?P<set>\[\^?)
                                 |(?P<esc>\\.)
                                 |(?P<dot>\.)
                                 |(?P<done>/))""", re.VERBOSE)
    _RE_SET = re.compile(r"^(\]|-|\\?.)")

    def __init__(self, pstate):
        name = "%s.[regex (line %d #%d)]" % (pstate.prefix, pstate.line_no, pstate.implicit())
        ConcatSymbol.__init__(self, name, pstate, no_prefix=True)
        self.can_terminate = True

    def _impl_name(self, n_implicit):
        name = "%s.%d]" % (self.name[:-1], n_implicit[0])
        n_implicit[0] += 1
        return name

    def new_text(self, value, n_implicit, pstate):
        self.append(TextSymbol(self._impl_name(n_implicit), value, pstate, no_prefix=True).name)

    def new_textchoice(self, alpha, n_implicit, pstate):
        self.append(_TextChoiceSymbol(self._impl_name(n_implicit), alpha, pstate, no_prefix=True).name)

    def add_repeat(self, min_, max_, n_implicit, pstate):
        rep = RepeatSymbol(self._impl_name(n_implicit), min_, max_, pstate, no_prefix=True)
        rep.append(self.pop())
        self.append(rep.name)

    @staticmethod
    def parse(defn, pstate):
        result = RegexSymbol(pstate)
        n_implicit = [0]
        if defn[0] != "/":
            raise ParseError("Regex definitions must begin with /", pstate)
        defn = defn[1:]
        while defn:
            match = RegexSymbol._RE_PARSE.match(defn)
            if match is None:
                result.new_text(defn[0], n_implicit, pstate)
                defn = defn[1:]
            elif match.group("set"):
                inverse = len(match.group("set")) == 2
                defn = defn[match.end(0):]
                alpha = []
                in_range = False
                while defn:
                    match = RegexSymbol._RE_SET.match(defn)
                    if match.group(0) == "]":
                        if in_range:
                            alpha.append('-')
                        defn = defn[match.end(0):]
                        break
                    elif match.group(0) == "-":
                        if in_range or not alpha:
                            raise ParseError("Parse error in regex at: %s" % defn, pstate)
                        in_range = True
                    else:
                        if match.group(0).startswith("\\"):
                            alpha.append(TextSymbol.ESCAPES.get(match.group(0)[1], match.group(0)[1]))
                        else:
                            alpha.append(match.group(0))
                        if in_range:
                            start = ord(alpha[-2])
                            end = ord(alpha[-1]) + 1
                            if start >= end:
                                raise ParseError("Empty range in regex at: %s" % defn, pstate)
                            alpha.extend(chr(letter) for letter in range(ord(alpha[-2]), ord(alpha[-1]) + 1))
                            in_range = False
                    defn = defn[match.end(0):]
                else:
                    raise ParseError("Unterminated set in regex", pstate)
                alpha = set(alpha)
                if inverse:
                    alpha = set(RegexSymbol._REGEX_ALPHABET) - alpha
                result.new_textchoice("".join(alpha), n_implicit, pstate)
            elif match.group("done"):
                return result, defn[match.end(0):]
            elif match.group("dot"):
                try:
                    pstate.grmr.symtab["[regex alpha]"]
                except KeyError:
                    sym = _TextChoiceSymbol("[regex alpha]", RegexSymbol._REGEX_ALPHABET, pstate, no_prefix=True)
                    sym.line_no = 0
                result.append("[regex alpha]")
                defn = defn[match.end(0):]
            elif match.group("esc"):
                result.new_text(TextSymbol.ESCAPES.get(match.group(0)[1], match.group(0)[1]), n_implicit, pstate)
                defn = defn[match.end(0):]
            else: # repeat
                if not len(result) or isinstance(pstate.grmr.symtab[result[-1]], RepeatSymbol):
                    raise ParseError("Error parsing regex, unexpected repeat at: %s" % defn, pstate)
                if match.group("a"):
                    min_ = int(match.group("a"))
                    max_ = int(match.group("b")) if match.group("b") else min_
                else:
                    min_, max_ = 0, 1
                result.add_repeat(min_, max_, n_implicit, pstate)
                defn = defn[match.end(0):]
        raise ParseError("Unterminated regular expression", pstate)


class RepeatSymbol(ConcatSymbol):
    """Repeat subsymbols a random number of times.

       ::

           SymbolName      {Min,Max}   SubSymbol        (named)
           SymbolName      ?           SubSymbol        (named)
           ...             ... SubSymbol {Min,Max}      (inline)
           ...             ... SubSymbol ?              (inline)

       Defines a repetition of subsymbols. The number of repetitions is at most ``Max``, and at minimum ``Min``.
       ``?`` is shorthand for {0,1}.
    """

    def __init__(self, name, min_, max_, pstate, no_prefix=False):
        name = "%s.%s" % (pstate.prefix, name) if not no_prefix else name
        ConcatSymbol.__init__(self, name, pstate, no_prefix=True)
        self.min_, self.max_ = min_, max_

    def normalize(self, grmr):
        pass

    def generate(self, gstate):
        if gstate.grmr.is_limit_exceeded(gstate.length):
            if not self.can_terminate:
                return # chop the output. this isn't great, but not much choice
            reps = self.min_
        else:
            reps = random.randint(self.min_, random.randint(self.min_, self.max_)) # roughly betavariate(0.75, 2.25)
        gstate.symstack.extend(reps * tuple(reversed(self)))


class RepeatSampleSymbol(RepeatSymbol):
    """
     **Repeat Unique**:

            ::

                SymbolName      <Min,Max>   SubSymbol

        Defines a repetition of a sub-symbol. The number of repetitions is at most ``Max``, and at minimum ``Min``.
        The sub-symbol must be a single ``ChoiceSymbol``, and the generated repetitions will be unique from the
        choices in the sub-symbol.
    """

    def __init__(self, name, min_, max_, pstate, no_prefix=False):
        RepeatSymbol.__init__(self, name, min_, max_, pstate, no_prefix)
        self.sample_idx = None

    def normalize(self, grmr):
        num_choices = 0
        for i, child in enumerate(self):
            if isinstance(grmr.symtab[child], ChoiceSymbol):
                num_choices += 1
                self.sample_idx = i
            elif isinstance(grmr.symtab[child], (TextSymbol, BinSymbol)):
                pass # allowed
            else:
                raise IntegrityError("RepeatSampleSymbol %s has invalid child type: %s(%s)"
                                     % (self.name, type(grmr.symtab[child]).__name__, child),
                                     self.line_no)
        if num_choices != 1:
            raise IntegrityError("RepeatSampleSymbol %s must have one ChoiceSymbol in its children, got %d"
                                 % (self.name, num_choices), self.line_no)

    def generate(self, gstate):
        if gstate.grmr.is_limit_exceeded(gstate.length):
            if not self.can_terminate:
                return # chop the output. this isn't great, but not much choice
            reps = self.min_
        else:
            reps = random.randint(self.min_, random.randint(self.min_, self.max_)) # roughly betavariate(0.75, 2.25)
        try:
            pre = self[:self.sample_idx]
            post = self[self.sample_idx + 1:]
            for selection in reversed(gstate.grmr.symtab[self[self.sample_idx]].sample(reps)):
                gstate.symstack.extend(reversed(pre + selection + post))
        except AssertionError as err:
            raise GenerationError(err, gstate)


class TextSymbol(_Symbol):
    """Text string

       ::

           SymbolName      'some text'
           SymbolName      "some text"

       A text symbol is a string generated verbatim in the output. A few escape codes are recognized:
           * ``\\t``  horizontal tab (ASCII 0x09)
           * ``\\n``   line feed (ASCII 0x0A)
           * ``\\v``  vertical tab (ASCII 0x0B)
           * ``\\r``  carriage return (ASCII 0x0D)
       Any other character preceded by backslash will appear in the output without the backslash (including backslash,
       single quote, and double quote).
    """

    _RE_QUOTE = re.compile(r"""(?P<end>["'])|\\(?P<esc>.)""")
    ESCAPES = {"f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}

    def __init__(self, name, value, pstate, no_prefix=False, no_add=False):
        if name is None:
            name = "[text (line %d #%d)]" % (pstate.line_no, pstate.implicit() if not no_add else -1)
        name = "%s.%s" % (pstate.prefix, name) if not no_prefix else name
        _Symbol.__init__(self, name, pstate, no_add=no_add)
        self.value = str(value)
        self.can_terminate = True

    def generate(self, gstate):
        gstate.append(self.value)

    @staticmethod
    def parse(defn, pstate, no_add=False):
        qchar, defn = defn[0], defn[1:]
        if qchar not in "'\"":
            raise ParseError("Error parsing string, expected \" or ' at: %s%s" % (qchar, defn), pstate)
        out, last = [], 0
        for match in TextSymbol._RE_QUOTE.finditer(defn):
            out.append(defn[last:match.start(0)])
            last = match.end(0)
            if match.group("end") == qchar:
                break
            elif match.group("end"):
                out.append(match.group("end"))
            else:
                out.append(TextSymbol.ESCAPES.get(match.group("esc"), match.group("esc")))
        else:
            raise ParseError("Unterminated string literal!", pstate)
        defn = defn[last:]
        sym = TextSymbol(None, "".join(out), pstate, no_add=no_add)
        return sym, defn


class _TextChoiceSymbol(TextSymbol):

    def generate(self, gstate):
        gstate.append(random.choice(self.value))


def main():
    argp = argparse.ArgumentParser(description="Generate a testcase from a grammar")
    argp.add_argument("input", type=argparse.FileType('r'), help="Input grammar definition")
    argp.add_argument("output", type=argparse.FileType('w'), nargs="?", default=sys.stdout, help="Output testcase")
    argp.add_argument("-f", "--function", action="append", nargs=2, default=[],
                      help="Function used in the grammar (eg. -f filter lambda x:x.replace('x','y')")
    argp.add_argument("-l", "--limit", type=int, default=DEFAULT_LIMIT, help="Set a generation limit (roughly)")
    args = argp.parse_args()
    args.function = {func: eval(defn) for (func, defn) in args.function}
    args.output.write(Grammar(args.input, limit=args.limit, **args.function).generate())

