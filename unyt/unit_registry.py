"""
A registry for units that can be added to and modified.


"""

# -----------------------------------------------------------------------------
# Copyright (c) 2018, yt Development Team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the LICENSE file, distributed with this software.
# -----------------------------------------------------------------------------


import json

from unyt.exceptions import (
    SymbolNotFoundError,
    UnitParseError,
)
from unyt._unit_lookup_table import (
    default_unit_symbol_lut,
    unit_prefixes,
    latex_prefixes,
)
from hashlib import md5
import six
from sympy import (
    sympify,
    srepr,
)


class UnitRegistry:
    """A registry for unit symbols"""
    _unit_system_id = None

    def __init__(self, add_default_symbols=True, lut=None):
        self._unit_object_cache = {}
        if lut:
            self.lut = lut
        else:
            self.lut = {}

        if add_default_symbols:
            self.lut.update(default_unit_symbol_lut)

    def __getitem__(self, key):
        return self.lut[key]

    def __contains__(self, item):
        if str(item) in self.lut:
            return True
        try:
            _lookup_unit_symbol(str(item), self.lut)
            return True
        except UnitParseError:
            return False

    def pop(self, item):
        if item in self.lut:
            del self.lut[item]

    @property
    def unit_system_id(self):
        """
        This is a unique identifier for the unit registry created
        from a FNV hash. It is needed to register a dataset's code
        unit system in the unit system registry.
        """
        if self._unit_system_id is None:
            hash_data = bytearray()
            for k, v in sorted(self.lut.items()):
                hash_data.extend(k.encode('ascii'))
                hash_data.extend(repr(v).encode('ascii'))
            m = md5()
            m.update(hash_data)
            self._unit_system_id = str(m.hexdigest())
        return self._unit_system_id

    @property
    def prefixable_units(self):
        return [u for u in self.lut if self.lut[u][4]]

    def add(self, symbol, base_value, dimensions, tex_repr=None, offset=None,
            prefixable=False):
        """
        Add a symbol to this registry.

        Parameters
        ----------

        symbol : str
           The name of the unit
        base_value : float
           The scaling from the units value to the equivalent SI unit
           with the same dimensions
        dimensions : expr
           The dimensions of the unit
        tex_repr : str, optional
           The LaTeX representation of the unit. If not provided a LaTeX
           representation is automatically generated from the name of
           the unit.
        offset : float, optional
           If set, the zero-point offset to apply to the unit to convert
           to SI. This is mostly used for units like Farhenheit and
           Celcius that are not defined on an absolute scale.
        prefixable : bool
           If True, then SI-prefix versions of the unit will be created
           along with the unit itself.

        """
        from unyt.unit_object import _validate_dimensions

        self._unit_system_id = None

        # Validate
        if not isinstance(base_value, float):
            raise UnitParseError("base_value (%s) must be a float, got a %s."
                                 % (base_value, type(base_value)))

        if offset is not None:
            if not isinstance(offset, float):
                raise UnitParseError(
                    "offset value (%s) must be a float, got a %s."
                    % (offset, type(offset)))
        else:
            offset = 0.0

        _validate_dimensions(dimensions)

        if tex_repr is None:
            # make educated guess that will look nice in most cases
            tex_repr = r"\rm{" + symbol.replace('_', '\ ') + "}"

        # Add to lut
        self.lut[symbol] = (
            base_value, dimensions, offset, tex_repr, prefixable)

    def remove(self, symbol):
        """
        Remove the entry for the unit matching `symbol`.

        Parameters
        ----------

        symbol : str
           The name of the unit symbol to remove from the registry.

        """
        self._unit_system_id = None

        if symbol not in self.lut:
            raise SymbolNotFoundError(
                "Tried to remove the symbol '%s', but it does not exist "
                "in this registry." % symbol)

        del self.lut[symbol]

    def modify(self, symbol, base_value):
        """
        Change the base value of a unit symbol.  Useful for adjusting code
        units after parsing parameters.

        Parameters
        ----------

        symbol : str
           The name of the symbol to modify
        base_value : float
           The new base_value for the symbol.

        """
        self._unit_system_id = None

        if symbol not in self.lut:
            raise SymbolNotFoundError(
                "Tried to modify the symbol '%s', but it does not exist "
                "in this registry." % symbol)

        if hasattr(base_value, "in_base"):
            new_dimensions = base_value.units.dimensions
            base_value = base_value.in_base('mks')
            base_value = base_value.value
        else:
            new_dimensions = self.lut[symbol][1]

        self.lut[symbol] = ((float(base_value), new_dimensions) +
                            self.lut[symbol][2:])

    def keys(self):
        """
        Print out the units contained in the lookup table.

        """
        return self.lut.keys()

    def to_json(self):
        """
        Returns a json-serialized version of the unit registry
        """
        sanitized_lut = {}
        for k, v in six.iteritems(self.lut):
            san_v = list(v)
            repr_dims = srepr(v[1])
            san_v[1] = repr_dims
            sanitized_lut[k] = tuple(san_v)

        return json.dumps(sanitized_lut)

    @classmethod
    def from_json(cls, json_text):
        """
        Returns a UnitRegistry object from a json-serialized unit registry

        Parameters
        ----------

        json_text : str
           A string containing a json represention of a UnitRegistry
        """
        data = json.loads(json_text)
        lut = {}
        for k, v in six.iteritems(data):
            unsan_v = list(v)
            unsan_v[1] = sympify(v[1])
            lut[k] = tuple(unsan_v)

        return cls(lut=lut, add_default_symbols=False)

    def list_same_dimensions(self, unit_object):
        """
        Return a list of base unit names that this registry knows about that
        are of equivalent dimensions to *unit_object*.
        """
        equiv = [k for k, v in self.lut.items()
                 if v[1] is unit_object.dimensions]
        equiv = list(sorted(set(equiv)))
        return equiv


def _lookup_unit_symbol(symbol_str, unit_symbol_lut):
    """
    Searches for the unit data tuple corresponding to the given symbol.

    Parameters
    ----------
    symbol_str : str
        The unit symbol to look up.
    unit_symbol_lut : dict
        Dictionary with symbols as keys and unit data tuples as values.

    """
    if symbol_str in unit_symbol_lut:
        # lookup successful, return the tuple directly
        return unit_symbol_lut[symbol_str]

    # could still be a known symbol with a prefix
    possible_prefix = symbol_str[0]

    if symbol_str[:2] == 'da':
        possible_prefix = 'da'

    if possible_prefix in unit_prefixes:
        # the first character could be a prefix, check the rest of the symbol
        symbol_wo_pref = symbol_str[1:]

        # deca is the only prefix with length 2
        if symbol_str[:2] == 'da':
            symbol_wo_pref = symbol_str[2:]
            possible_prefix = 'da'

        prefixable_units = [u for u in unit_symbol_lut
                            if unit_symbol_lut[u][4]]

        unit_is_si_prefixable = (symbol_wo_pref in unit_symbol_lut and
                                 symbol_wo_pref in prefixable_units)

        if unit_is_si_prefixable is True:
            # lookup successful, it's a symbol with a prefix
            unit_data = unit_symbol_lut[symbol_wo_pref]
            prefix_value = unit_prefixes[possible_prefix]

            if possible_prefix in latex_prefixes:
                latex_repr = symbol_str.replace(
                    possible_prefix, '{'+latex_prefixes[possible_prefix]+'}')
            else:
                # Need to add some special handling for comoving units
                # this is fine for now, but it wouldn't work for a general
                # unit that has an arbitrary LaTeX representation
                if symbol_wo_pref != 'cm' and symbol_wo_pref.endswith('cm'):
                    sub_symbol_wo_prefix = symbol_wo_pref[:-2]
                    sub_symbol_str = symbol_str[:-2]
                else:
                    sub_symbol_wo_prefix = symbol_wo_pref
                    sub_symbol_str = symbol_str
                latex_repr = unit_data[3].replace(
                    '{' + sub_symbol_wo_prefix + '}',
                    '{' + sub_symbol_str + '}')

            # Leave offset and dimensions the same, but adjust scale factor and
            # LaTeX representation
            ret = (unit_data[0] * prefix_value, unit_data[1], unit_data[2],
                   latex_repr, False)

            unit_symbol_lut[symbol_str] = ret

            return ret

    # no dice
    raise UnitParseError("Could not find unit symbol '%s' in the provided "
                         "symbols." % symbol_str)


#: The default unit registry
default_unit_registry = UnitRegistry()
