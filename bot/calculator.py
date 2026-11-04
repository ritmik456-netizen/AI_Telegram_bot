import ast
import math
import operator
import re

MAX_INT_BITS = 4000


class CalcError(ValueError):
    pass


def _safe_pow(base, exp):
    if isinstance(base, (int, float)) and isinstance(exp, (int, float)) and abs(base) > 1 and exp > 0:
        if exp * math.log10(abs(base)) > 1000:
            raise CalcError("слишком большое число")
    return operator.pow(base, exp)


def _safe_factorial(n):
    if not float(n).is_integer() or n < 0:
        raise CalcError("факториал определён только для целых неотрицательных чисел")
    if n > 300:
        raise CalcError("слишком большой факториал")
    return math.factorial(int(n))


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: _safe_pow,
}

_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_FUNCS = {
    "sqrt": math.sqrt,
    "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "tg": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "ln": math.log,
    "lg": math.log10,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "factorial": _safe_factorial,
    "fact": _safe_factorial,
}

_CONSTS = {"pi": math.pi, "π": math.pi, "e": math.e}

_EXPRESSION_RE = re.compile(r"^[\d\s+\-*/^().,×÷:·√π]+$")
_HAS_OPERATOR_RE = re.compile(r"\d\s*[+\-*/^×÷:·]\s*[\d(√π]|√")


def _normalize(expr: str) -> str:
    expr = expr.strip().rstrip("=").strip()
    expr = re.sub(r"(?<=\d),(?=\d)", ".", expr)
    expr = re.sub(r"√\s*(\d+(?:\.\d+)?|\([^()]*\))", r"sqrt(\1)", expr)
    for src, dst in (("^", "**"), ("×", "*"), ("·", "*"), ("÷", "/"), (":", "/")):
        expr = expr.replace(src, dst)
    return expr


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        result = _BIN_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(result, int) and result.bit_length() > MAX_INT_BITS:
            raise CalcError("слишком большое число")
        return result
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCS
        and not node.keywords
    ):
        return _FUNCS[node.func.id](*(_eval(arg) for arg in node.args))
    raise CalcError("неподдерживаемое выражение")


def _format(value) -> str:
    if isinstance(value, complex):
        raise CalcError("результат не является действительным числом")
    if isinstance(value, float):
        if math.isinf(value) or math.isnan(value):
            raise CalcError("результат не определён")
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.10g}"
    return str(value)


def calculate(expr: str) -> str:
    normalized = _normalize(expr)
    if not normalized:
        raise CalcError("пустое выражение")
    try:
        tree = ast.parse(normalized, mode="eval")
        return _format(_eval(tree))
    except CalcError:
        raise
    except ZeroDivisionError:
        raise CalcError("деление на ноль") from None
    except (SyntaxError, TypeError):
        raise CalcError("не удалось разобрать выражение") from None
    except (ValueError, OverflowError) as exc:
        raise CalcError(f"недопустимое значение ({exc})") from None


def looks_like_expression(text: str) -> bool:
    """Простой пример вроде «2+2*3» или «√16 + 5^2» — его можно посчитать без нейросети."""
    text = text.strip().rstrip("=?").strip()
    return bool(_EXPRESSION_RE.match(text) and _HAS_OPERATOR_RE.search(text))
