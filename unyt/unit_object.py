"""
A class that represents a unit symbol.


"""

# -----------------------------------------------------------------------------
# Copyright (c) 2018, yt Development Team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the LICENSE file, distributed with this software.
# -----------------------------------------------------------------------------


import copy
try:
    from functools import lru_cache
except ImportError:
    from backports.functools_lru_cache import lru_cache
from keyword import iskeyword as _iskeyword
import numpy as np
from numbers import Number as numeric_type
from six import text_type
import token

from sympy import (
    Expr,
    Mul,
    Add,
    Number,
    Pow,
    Symbol,
    Integer,
    Float,
    Basic,
    Rational,
    sqrt
)
from sympy.core.numbers import One
from sympy import (
    sympify,
    latex
)
from sympy.parsing.sympy_parser import (
    parse_expr,
    auto_number,
    rationalize
)

from unyt.dimensions import (
    angle,
    base_dimensions,
    dimensionless,
    temperature,
    current_mks,
)
import unyt.dimensions as dims
from unyt.equivalencies import equivalence_registry
from unyt.exceptions import (
    InvalidUnitOperation,
    MKSCGSConversionError,
    UnitConversionError,
    UnitsNotReducible,
)
from unyt._physical_ratios import speed_of_light_cm_per_s
from unyt.unit_registry import (
    default_unit_registry,
    _lookup_unit_symbol,
    UnitRegistry,
    UnitParseError,
)

sympy_one = sympify(1)

global_dict = {
    'Symbol': Symbol,
    'Integer': Integer,
    'Float': Float,
    'Rational': Rational,
    'sqrt': sqrt
}


def _sanitize_unit_system(unit_system, obj):
    from unyt.unit_systems import unit_system_registry
    if hasattr(unit_system, "unit_registry"):
        unit_system = unit_system.unit_registry.unit_system_id
    elif unit_system == "code":
        unit_system = obj.registry.unit_system_id
    return unit_system_registry[str(unit_system)]


def _auto_positive_symbol(tokens, local_dict, global_dict):
    """
    Inserts calls to ``Symbol`` for undefined variables.
    Passes in positive=True as a keyword argument.
    Adapted from sympy.sympy.parsing.sympy_parser.auto_symbol
    """
    result = []
    prevTok = (None, None)

    tokens.append((None, None))  # so zip traverses all tokens
    for tok, nextTok in zip(tokens, tokens[1:]):
        tokNum, tokVal = tok
        nextTokNum, nextTokVal = nextTok
        if tokNum == token.NAME:
            name = tokVal

            if (name in ['True', 'False', 'None']
                or _iskeyword(name)
                or name in local_dict
                # Don't convert attribute access
                or (prevTok[0] == token.OP and prevTok[1] == '.')
                # Don't convert keyword arguments
                or (prevTok[0] == token.OP and prevTok[1] in ('(', ',')
                    and nextTokNum == token.OP and nextTokVal == '=')):
                result.append((token.NAME, name))
                continue
            elif name in global_dict:
                obj = global_dict[name]
                if isinstance(obj, (Basic, type)) or callable(obj):
                    result.append((token.NAME, name))
                    continue

            result.extend([
                (token.NAME, 'Symbol'),
                (token.OP, '('),
                (token.NAME, repr(str(name))),
                (token.OP, ','),
                (token.NAME, 'positive'),
                (token.OP, '='),
                (token.NAME, 'True'),
                (token.OP, ')'),
            ])
        else:
            result.append((tokNum, tokVal))

        prevTok = (tokNum, tokVal)

    return result


def _get_latex_representation(expr, registry):
    symbol_table = {}
    for ex in expr.free_symbols:
        try:
            symbol_table[ex] = registry.lut[str(ex)][3]
        except KeyError:
            symbol_table[ex] = r"\rm{" + str(ex).replace('_', '\ ') + "}"

    # invert the symbol table dict to look for keys with identical values
    invert_symbols = {}
    for key, value in symbol_table.items():
        if value not in invert_symbols:
            invert_symbols[value] = [key]
        else:
            invert_symbols[value].append(key)

    # if there are any units with identical latex representations, substitute
    # units to avoid  uncanceled terms in the final latex expression.
    for val in invert_symbols:
        symbols = invert_symbols[val]
        for i in range(1, len(symbols)):
            expr = expr.subs(symbols[i], symbols[0])
    prefix = None
    l_expr = expr
    if isinstance(expr, Mul):
        coeffs = expr.as_coeff_Mul()
        if coeffs[0] == 1 or not isinstance(coeffs[0], Float):
            l_expr = coeffs[1]
        else:
            l_expr = coeffs[1]
            prefix = Float(coeffs[0], 2)
    latex_repr = latex(l_expr, symbol_names=symbol_table, mul_symbol="dot",
                       fold_frac_powers=True, fold_short_frac=True)

    if prefix is not None:
        latex_repr = latex(prefix, mul_symbol="times") + '\\ ' + latex_repr

    if latex_repr == '1':
        return ''
    else:
        return latex_repr


unit_text_transform = (_auto_positive_symbol, rationalize, auto_number)


class Unit(object):
    """
    A symbolic unit, using sympy functionality. We only add "dimensions" so
    that sympy understands relations between different units.

    """

    # Set some assumptions for sympy.
    is_positive = True    # make sqrt(m**2) --> m
    is_commutative = True
    is_number = False

    # caches for imports
    _ua = None
    _uq = None

    __array_priority__ = 3.0

    def __new__(cls, unit_expr=sympy_one, base_value=None, base_offset=0.0,
                dimensions=None, registry=None, latex_repr=None):
        """
        Create a new unit. May be an atomic unit (like a gram) or combinations
        of atomic units (like g / cm**3).

        Parameters
        ----------
        unit_expr : Unit object, sympy.core.expr.Expr object, or str
            The symbolic unit expression.
        base_value : float
            The unit's value in yt's base units.
        base_offset : float
            The offset necessary to normalize temperature units to a common
            zero point.
        dimensions : sympy.core.expr.Expr
            A sympy expression representing the dimensionality of this unit.
            It must contain only mass, length, time, temperature and angle
            symbols.
        registry : UnitRegistry object
            The unit registry we use to interpret unit symbols.
        latex_repr : string
            A string to render the unit as LaTeX

        """
        unit_cache_key = None
        # Parse a text unit representation using sympy's parser
        if isinstance(unit_expr, (str, bytes, text_type)):
            if isinstance(unit_expr, bytes):
                unit_expr = unit_expr.decode("utf-8")

            # this cache substantially speeds up unit conversions
            if registry and unit_expr in registry._unit_object_cache:
                return registry._unit_object_cache[unit_expr]
            unit_cache_key = unit_expr
            if not unit_expr:
                # Bug catch...
                # if unit_expr is an empty string, parse_expr fails hard...
                unit_expr = "1"
            try:
                unit_expr = parse_expr(unit_expr, global_dict=global_dict,
                                       transformations=unit_text_transform)
            except SyntaxError as e:
                msg = ("Unit expression %s raised an error "
                       "during parsing:\n%s" % (unit_expr, repr(e)))
                raise UnitParseError(msg)
        # Simplest case. If user passes a Unit object, just use the expr.
        elif isinstance(unit_expr, Unit):
            # grab the unit object's sympy expression.
            unit_expr = unit_expr.expr
        elif hasattr(unit_expr, 'units') and hasattr(unit_expr, 'value'):
            # something that looks like a unyt_array, grab the unit and value
            if unit_expr.shape != ():
                raise UnitParseError(
                    'Cannot create a unit from a non-scalar unyt_array, '
                    'received: %s' % (unit_expr, ))
            value = unit_expr.value
            if value == 1:
                unit_expr = unit_expr.units.expr
            else:
                unit_expr = unit_expr.value*unit_expr.units.expr
        # Make sure we have an Expr at this point.
        if not isinstance(unit_expr, Expr):
            raise UnitParseError("Unit representation must be a string or "
                                 "sympy Expr. %s has type %s."
                                 % (unit_expr, type(unit_expr)))

        # this is slightly faster if unit_expr is the same object as
        # sympy_one than just checking for == equality
        is_one = (unit_expr is sympy_one or unit_expr == sympy_one)
        if dimensions is None and is_one:
            dimensions = dimensionless

        if registry is None:
            # Caller did not set the registry, so use the default.
            registry = default_unit_registry

        # done with argument checking...

        # see if the unit is atomic.
        is_atomic = False
        if isinstance(unit_expr, Symbol):
            is_atomic = True

        #
        # check base_value and dimensions
        #

        if base_value is not None:
            # check that base_value is a float or can be converted to one
            try:
                base_value = float(base_value)
            except ValueError:
                raise UnitParseError("Could not use base_value as a float. "
                                     "base_value is '%s' (type %s)."
                                     % (base_value, type(base_value)))

            # check that dimensions is valid
            if dimensions is not None:
                _validate_dimensions(dimensions)
        else:
            # lookup the unit symbols
            unit_data = _get_unit_data_from_expr(unit_expr, registry.lut)
            base_value = unit_data[0]
            dimensions = unit_data[1]
            if len(unit_data) > 2:
                base_offset = unit_data[2]
                latex_repr = unit_data[3]
            else:
                base_offset = 0.0

        # Create obj with superclass construct.
        obj = super(Unit, cls).__new__(cls)

        # Attach attributes to obj.
        obj.expr = unit_expr
        obj.is_atomic = is_atomic
        obj.base_value = base_value
        obj.base_offset = base_offset
        obj.dimensions = dimensions
        obj._latex_repr = latex_repr
        obj.registry = registry

        # if we parsed a string unit expression, cache the result
        # for faster lookup later
        if unit_cache_key is not None:
            registry._unit_object_cache[unit_cache_key] = obj

        # Return `obj` so __init__ can handle it.

        return obj

    @property
    def latex_repr(self):
        """A LaTeX representation for the unit

        Examples
        --------
        >>> from unyt import g, cm
        >>> (g/cm**3).units.latex_repr
        '\\\\frac{\\\\rm{g}}{\\\\rm{cm}^{3}}'
        """
        if self._latex_repr is not None:
            return self._latex_repr
        if self.expr.is_Atom:
            expr = self.expr
        else:
            expr = self.expr.copy()
        self._latex_repr = _get_latex_representation(expr, self.registry)
        return self._latex_repr

    @property
    def units(self):
        return self

    def __hash__(self):
        return super(Unit, self).__hash__()

    # end sympy conventions

    def __repr__(self):
        if self.expr == sympy_one:
            return "(dimensionless)"
        # @todo: don't use dunder method?
        return self.expr.__repr__()

    def __str__(self):
        if self.expr == sympy_one:
            return "dimensionless"
        # @todo: don't use dunder method?
        return self.expr.__str__()

    #
    # Start unit operations
    #

    def __add__(self, u):
        raise InvalidUnitOperation("addition with unit objects is not allowed")

    def __radd__(self, u):
        raise InvalidUnitOperation("addition with unit objects is not allowed")

    def __sub__(self, u):
        raise InvalidUnitOperation(
            "subtraction with unit objects is not allowed")

    def __rsub__(self, u):
        raise InvalidUnitOperation(
            "subtraction with unit objects is not allowed")

    def __iadd__(self, u):
        raise InvalidUnitOperation(
            "in-place operations with unit objects are not allowed")

    def __isub__(self, u):
        raise InvalidUnitOperation(
            "in-place operations with unit objects are not allowed")

    def __imul__(self, u):
        raise InvalidUnitOperation(
            "in-place operations with unit objects are not allowed")

    def __idiv__(self, u):
        raise InvalidUnitOperation(
            "in-place operations with unit objects are not allowed")

    def __itruediv__(self, u):
        raise InvalidUnitOperation(
            "in-place operations with unit objects are not allowed")

    def __rmul__(self, u):
        return self.__mul__(u)

    def __mul__(self, u):
        """ Multiply Unit with u (Unit object). """
        if self._ua is None:
            # cache the imported object to avoid cost of repeated imports
            from unyt.array import unyt_quantity, unyt_array
            self._ua = unyt_array
            self._uq = unyt_quantity
        if not isinstance(u, Unit):
            cls = type(u)
            if ((cls in (np.ndarray, np.matrix, np.ma.masked_array) or
                 isinstance(u, (numeric_type, list, tuple)))):
                try:
                    units = u.units*self
                except AttributeError:
                    units = self
                data = np.array(u, dtype='float64')
                if data.shape == ():
                    return self._uq(data, units, bypass_validation=True)
                return self._ua(data, units, bypass_validation=True)
            elif isinstance(u, self._ua):
                return cls(u, u.units*self, bypass_validation=True)
            else:
                raise InvalidUnitOperation(
                    "Tried to multiply a Unit object with '%s' (type %s). "
                    "This behavior is undefined." % (u, type(u)))

        base_offset = 0.0
        if self.base_offset or u.base_offset:
            if u.dimensions in (temperature, angle) and self.is_dimensionless:
                base_offset = u.base_offset
            elif (self.dimensions in (temperature, angle) and
                  u.is_dimensionless):
                base_offset = self.base_offset
            else:
                raise InvalidUnitOperation(
                    "Quantities with dimensions of angle or units of "
                    "Fahrenheit or Celsius cannot be multiplied.")

        return Unit(self.expr * u.expr,
                    base_value=(self.base_value * u.base_value),
                    base_offset=base_offset,
                    dimensions=(self.dimensions * u.dimensions),
                    registry=self.registry)

    def __div__(self, u):
        """ Divide Unit by u (Unit object). """
        if not isinstance(u, Unit):
            if isinstance(u, (numeric_type, list, tuple, np.ndarray)):
                from unyt.array import unyt_quantity
                return unyt_quantity(1.0, self)/u
            else:
                raise InvalidUnitOperation(
                    "Tried to divide a Unit object by '%s' (type %s). This "
                    "behavior is undefined." % (u, type(u)))

        base_offset = 0.0
        if self.base_offset or u.base_offset:
            if self.dimensions in (temperature, angle) and u.is_dimensionless:
                base_offset = self.base_offset
            else:
                raise InvalidUnitOperation(
                    "Quantities with units of Farhenheit "
                    "and Celsius cannot be divided.")

        return Unit(self.expr / u.expr,
                    base_value=(self.base_value / u.base_value),
                    base_offset=base_offset,
                    dimensions=(self.dimensions / u.dimensions),
                    registry=self.registry)

    __truediv__ = __div__

    def __rdiv__(self, u):
        return u * self**-1

    def __rtruediv__(self, u):
        return u * self**-1

    def __pow__(self, p):
        """ Take Unit to power p (float). """
        try:
            p = Rational(str(p)).limit_denominator()
        except (ValueError, TypeError):
            raise InvalidUnitOperation("Tried to take a Unit object to the "
                                       "power '%s' (type %s). Failed to cast "
                                       "it to a float." % (p, type(p)))

        return Unit(self.expr**p, base_value=(self.base_value**p),
                    dimensions=(self.dimensions**p),
                    registry=self.registry)

    def __eq__(self, u):
        """ Test unit equality. """
        if not isinstance(u, Unit):
            return False
        return (self.base_value == u.base_value and
                self.dimensions == u.dimensions)

    def __ne__(self, u):
        """ Test unit inequality. """
        if not isinstance(u, Unit):
            return True
        if self.base_value != u.base_value:
            return True
        # use 'is' comparison dimensions to avoid expensive sympy operation
        if self.dimensions is u.dimensions:
            return False
        # fall back to expensive sympy comparison
        return self.dimensions != u.dimensions

    def copy(self):
        return copy.deepcopy(self)

    def __deepcopy__(self, memodict=None):
        expr = str(self.expr)
        base_value = copy.deepcopy(self.base_value)
        base_offset = copy.deepcopy(self.base_offset)
        dimensions = copy.deepcopy(self.dimensions)
        lut = copy.deepcopy(self.registry.lut)
        registry = UnitRegistry(lut=lut)
        return Unit(expr, base_value, base_offset, dimensions, registry)

    #
    # End unit operations
    #

    def same_dimensions_as(self, other_unit):
        """Test if the dimensions of *other_unit* are the same as this unit

        Examples
        --------
        >>> from unyt import Msun, kg, mile
        >>> Msun.units.same_dimensions_as(kg.units)
        True
        >>> Msun.units.same_dimensions_as(mile.units)
        False
        """
        # test first for 'is' equality to avoid expensive sympy operation
        if self.dimensions is other_unit.dimensions:
            return True
        return (self.dimensions / other_unit.dimensions) == sympy_one

    @property
    def is_dimensionless(self):
        """Is this a dimensionless unit?

        Returns
        -------
        True for a dimensionless unit, False otherwise

        Examples
        --------
        >>> from unyt import count, kg
        >>> count.units.is_dimensionless
        True
        >>> kg.units.is_dimensionless
        False
        """
        return self.dimensions is sympy_one

    @property
    def is_code_unit(self):
        """Is this a "code" unit?

        Returns
        -------
        True if the unit consists of atom units that being with "code".
        False otherwise

        """
        for atom in self.expr.atoms():
            if not (str(atom).startswith("code") or atom.is_Number):
                return False
        return True

    def list_equivalencies(self):
        """Lists the possible equivalencies associated with this unit object

        Examples
        --------
        >>> from unyt import km
        >>> km.units.list_equivalencies()
        spectral: length <-> spatial_frequency <-> frequency <-> energy
        schwarzschild: mass <-> length
        compton: mass <-> length
        """
        from unyt.equivalencies import equivalence_registry
        for k, v in equivalence_registry.items():
            if self.has_equivalent(k):
                print(v())

    def has_equivalent(self, equiv):
        """
        Check to see if this unit object as an equivalent unit in *equiv*.

        Example
        -------
        >>> from unyt import km
        >>> km.has_equivalent('spectral')
        True
        >>> km.has_equivalent('mass_energy')
        False
        """
        try:
            this_equiv = equivalence_registry[equiv]()
        except KeyError:
            raise KeyError("No such equivalence \"%s\"." % equiv)
        old_dims = self.dimensions
        return old_dims in this_equiv._dims

    def get_base_equivalent(self, unit_system="mks"):
        """Create and return dimensionally-equivalent units in a specified base.

        >>> from unyt import g, cm
        >>> (g/cm**3).get_base_equivalent('mks')
        kg/m**3
        >>> (g/cm**3).get_base_equivalent('solar')
        Mearth/AU**3
        """
        unit_system = _sanitize_unit_system(unit_system, self)
        try:
            conv_data = _check_em_conversion(
                self.units, registry=self.registry, unit_system=unit_system)
        except MKSCGSConversionError:
            raise UnitsNotReducible(self.units, unit_system)
        if any(conv_data):
            new_units, _ = _em_conversion(
                self, conv_data, unit_system=unit_system)
        else:
            new_units = unit_system[self.dimensions]
        return Unit(new_units, registry=self.registry)

    def get_cgs_equivalent(self):
        """Create and return dimensionally-equivalent cgs units.

        Example
        -------
        >>> from unyt import kg, m
        >>> (kg/m**3).get_cgs_equivalent()
        g/cm**3
        """
        return self.get_base_equivalent(unit_system="cgs")

    def get_mks_equivalent(self):
        """Create and return dimensionally-equivalent mks units.

        Example
        -------
        >>> from unyt import g, cm
        >>> (g/cm**3).get_mks_equivalent()
        kg/m**3
        """
        return self.get_base_equivalent(unit_system="mks")

    def get_conversion_factor(self, other_units):
        """Get the conversion factor and offset (if any) from one unit to another

        Parameters
        ----------
        other_units: unit object
           The units we want the conversion factor for

        Returns
        -------
        conversion_factor : float
            old_units / new_units
        offset : float or None
            Offset between this unit and the other unit. None if there is
            no offset.

        Examples
        --------
        >>> from unyt import km, cm, degree_fahrenheit, degree_celsius
        >>> km.get_conversion_factor(cm)
        (100000.0, None)
        >>> degree_celsius.get_conversion_factor(degree_fahrenheit)
        (1.7999999999999998, -31.999999999999886)
        """
        return _get_conversion_factor(self, other_units)

    def latex_representation(self):
        """A LaTeX representation for the unit

        Examples
        --------
        >>> from unyt import g, cm
        >>> (g/cm**3).latex_representation()
        '\\\\frac{\\\\rm{g}}{\\\\rm{cm}^{3}}'
        """
        return self.latex_repr

#
# Unit manipulation functions
#


# map from dimensions in one unit system to dimensions in other system,
# canonical unit to convert to in that system, and floating point
# conversion factor
em_conversions = {
    dims.charge_mks: (dims.charge_cgs, "esu", 0.1*speed_of_light_cm_per_s),
    dims.charge_cgs: (dims.charge_mks, "C", 10.0/speed_of_light_cm_per_s),
    dims.magnetic_field_mks: (dims.magnetic_field_cgs, "gauss", 1.0e4),
    dims.magnetic_field_cgs: (dims.magnetic_field_mks, "T", 1.0e-4),
    dims.current_mks: (
        dims.current_cgs, "statA", 0.1*speed_of_light_cm_per_s),
    dims.current_cgs: (dims.current_mks, "A", 10.0/speed_of_light_cm_per_s),
    dims.electric_potential_mks: (
        dims.electric_potential_cgs, "statV",
        1.0e-8*speed_of_light_cm_per_s),
    dims.electric_potential_cgs: (
        dims.electric_potential_mks, "V", 1.0e8/speed_of_light_cm_per_s),
    dims.resistance_mks: (
        dims.resistance_cgs, "statohm", 1.0e9/(speed_of_light_cm_per_s**2)),
    dims.resistance_cgs: (
        dims.resistance_mks, "ohm", 1.0e-9*speed_of_light_cm_per_s**2)
}


def _em_conversion(orig_units, conv_data, to_units=None, unit_system=None):
    """Convert between E&M & MKS base units.

    If orig_units is a CGS (or MKS) E&M unit, conv_data contains the
    corresponding MKS (or CGS) unit and scale factor converting between them.
    This must be done by replacing the expression of the original unit
    with the new one in the unit expression and multiplying by the scale
    factor.
    """
    conv_unit, canonical_unit, scale = conv_data
    if conv_unit is None:
        conv_unit = canonical_unit
    new_expr = orig_units.copy().expr.replace(
        orig_units.expr, scale*canonical_unit.expr)
    if unit_system is not None:
        # we don't know the to_units, so we get it directly from the
        # conv_data
        inter_expr = orig_units.copy().expr.replace(
            orig_units.expr, conv_unit.expr)
        to_units = Unit(inter_expr, registry=orig_units.registry)
    new_units = Unit(new_expr, registry=orig_units.registry)
    conv = new_units.get_conversion_factor(to_units)
    return to_units, conv


@lru_cache(maxsize=128, typed=False)
def _check_em_conversion(unit, to_unit=None, unit_system=None,
                         registry=None):
    """Check to see if the units contain E&M units

    This function supports unyt's ability to convert data to and from E&M
    electromagnetic units. However, this support is limited and only very
    simple unit expressions can be readily converted. This function
    to see if the unit is an atomic base unit that is present in the
    em_conversions dict. If it does not contain E&M units, the function
    returns an empty tuple. If it does contain an atomic E&M unit in
    the em_conversions dict, it returns a tuple containing the unit to convert
    to and scale factor. If it contains a more complicated E&M unit and we are
    trying to convert between CGS & MKS E&M units, it raises an error.
    """
    em_map = ()
    if unit == to_unit:
        return em_map
    if unit.dimensions in em_conversions:
        em_info = em_conversions[unit.dimensions]
        em_unit = Unit(em_info[1], registry=registry)
        if to_unit is None:
            cmks_in_unit = current_mks in unit.dimensions.atoms()
            cmks_in_unit_system = unit_system.units_map[current_mks]
            cmks_in_unit_system = cmks_in_unit_system is not None
            if cmks_in_unit and cmks_in_unit_system:
                em_map = (unit_system[unit.dimensions], unit, 1.0)
            else:
                em_map = (None, em_unit, em_info[2])
        elif to_unit.dimensions == em_unit.dimensions:
            em_map = (to_unit, em_unit, em_info[2])
    if em_map:
        return em_map
    for unit_atom in unit.expr.atoms():
        if unit_atom.is_Number:
            pass
        bu = str(unit_atom)
        budims = Unit(bu, registry=registry).dimensions
        if budims in em_conversions:
            conv_unit = em_conversions[budims][1]
            if to_unit is not None:
                for to_unit_atom in to_unit.expr.atoms():
                    bou = str(to_unit_atom)
                    if bou == conv_unit:
                        raise MKSCGSConversionError(unit)
            else:
                raise MKSCGSConversionError(unit)
    return em_map


def _get_conversion_factor(old_units, new_units):
    """
    Get the conversion factor between two units of equivalent dimensions. This
    is the number you multiply data by to convert from values in `old_units` to
    values in `new_units`.

    Parameters
    ----------
    old_units: str or Unit object
        The current units.
    new_units : str or Unit object
        The units we want.

    Returns
    -------
    conversion_factor : float
        `old_units / new_units`
    offset : float or None
        Offset between the old unit and new unit.

    """
    if old_units.dimensions != new_units.dimensions:
        raise UnitConversionError(old_units, old_units.dimensions,
                                  new_units, new_units.dimensions)
    ratio = old_units.base_value / new_units.base_value
    if old_units.base_offset == 0 and new_units.base_offset == 0:
        return (ratio, None)
    else:
        # the dimensions are the same, so both are temperatures, where
        # it's legal to convert units so no need to do error checking
        return ratio, ratio*old_units.base_offset - new_units.base_offset

#
# Helper functions
#


def _get_unit_data_from_expr(unit_expr, unit_symbol_lut):
    """
    Grabs the total base_value and dimensions from a valid unit expression.

    Parameters
    ----------
    unit_expr: Unit object, or sympy Expr object
        The expression containing unit symbols.
    unit_symbol_lut: dict
        Provides the unit data for each valid unit symbol.

    """
    # Now for the sympy possibilities
    if isinstance(unit_expr, Number):
        if unit_expr is sympy_one:
            return (1.0, sympy_one)
        return (float(unit_expr), sympy_one)

    if isinstance(unit_expr, Symbol):
        return _lookup_unit_symbol(str(unit_expr), unit_symbol_lut)

    if isinstance(unit_expr, Pow):
        unit_data = _get_unit_data_from_expr(
            unit_expr.args[0], unit_symbol_lut)
        power = unit_expr.args[1]
        if isinstance(power, Symbol):
            raise UnitParseError("Invalid unit expression '%s'." % unit_expr)
        conv = float(unit_data[0]**power)
        unit = unit_data[1]**power
        return (conv, unit)

    if isinstance(unit_expr, Mul):
        base_value = 1.0
        dimensions = 1
        for expr in unit_expr.args:
            unit_data = _get_unit_data_from_expr(expr, unit_symbol_lut)
            base_value *= unit_data[0]
            dimensions *= unit_data[1]

        return (float(base_value), dimensions)

    raise UnitParseError("Cannot parse for unit data from '%s'. Please supply"
                         " an expression of only Unit, Symbol, Pow, and Mul"
                         "objects." % str(unit_expr))


def _validate_dimensions(dimensions):
    if isinstance(dimensions, Mul):
        for dim in dimensions.args:
            _validate_dimensions(dim)
    elif isinstance(dimensions, Symbol):
        if dimensions not in base_dimensions:
            raise UnitParseError("Dimensionality expression contains an "
                                 "unknown symbol '%s'." % dimensions)
    elif isinstance(dimensions, Pow):
        if not isinstance(dimensions.args[1], Number):
            raise UnitParseError("Dimensionality expression '%s' contains a "
                                 "unit symbol as a power." % dimensions)
    elif isinstance(dimensions, (Add, Number)):
        if not isinstance(dimensions, One):
            raise UnitParseError("Only dimensions that are instances of Pow, "
                                 "Mul, or symbols in the base dimensions are "
                                 "allowed.  Got dimensions '%s'" % dimensions)
    elif not isinstance(dimensions, Basic):
        raise UnitParseError("Bad dimensionality expression '%s'." %
                             dimensions)


def _get_system_unit_string(dimensions, base_units):
    # The dimensions of a unit object is the product of the base dimensions.
    # Use sympy to factor the dimensions into base CGS unit symbols.
    units = []
    my_dims = dimensions.expand()
    if my_dims is dimensionless:
        return ""
    if my_dims in base_units:
        return base_units[my_dims]
    for factor in my_dims.as_ordered_factors():
        dim = list(factor.free_symbols)[0]
        unit_string = str(base_units[dim])
        if factor.is_Pow:
            power_string = "**(%s)" % factor.as_base_exp()[1]
        else:
            power_string = ""
        units.append("(%s)%s" % (unit_string, power_string))
    return " * ".join(units)


def define_unit(symbol, value, tex_repr=None, offset=None, prefixable=False,
                registry=None):
    """
    Define a new unit and add it to the default unit registry.

    Parameters
    ----------
    symbol : string
        The symbol for the new unit.
    value : tuple or ~unyt.array.unyt_quantity
        The definition of the new unit in terms of some other units. For
        example, one would define a new "mph" unit with ``(1.0, "mile/hr")``
        or with ``1.0*unyt.mile/unyt.hr``
    tex_repr : string, optional
        The LaTeX representation of the new unit. If one is not supplied, it
        will be generated automatically based on the symbol string.
    offset : float, optional
        The default offset for the unit. If not set, an offset of 0 is assumed.
    prefixable : boolean, optional
        Whether or not the new unit can use SI prefixes. Default: False
    registry : A ~unyt.unit_registry.UnitRegistry instance or None
        The unit registry to add the unit to. If None, then defaults to the
        global default unit registry. If registry is set to None then the
        unit object will be added as an attribute to the top-level :mod:`unyt`
        namespace to ease working with the newly defined unit. See the example
        below.

    Examples
    --------
    >>> from unyt import day
    >>> two_weeks = 14.0*day
    >>> one_day = 1.0*day
    >>> define_unit("fortnight", two_weeks)
    >>> from unyt import fortnight
    >>> print((3*fortnight)/one_day)
    42.0 dimensionless
    """
    from unyt.array import unyt_quantity, _iterable
    import unyt
    if registry is None:
        registry = default_unit_registry
    if symbol in registry:
        registry.pop(symbol)
    if not isinstance(value, unyt_quantity):
        if _iterable(value) and len(value) == 2:
            value = unyt_quantity(value[0], value[1])
        else:
            raise RuntimeError("\"value\" needs to be a quantity or "
                               "(value, unit) tuple!")
    base_value = float(value.in_base(unit_system='mks'))
    dimensions = value.units.dimensions
    registry.add(symbol, base_value, dimensions, prefixable=prefixable,
                 tex_repr=tex_repr, offset=offset)
    if registry is default_unit_registry:
        u = Unit(symbol, registry=registry)
        setattr(unyt, symbol, u)
