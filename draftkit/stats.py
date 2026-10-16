"""The handful of statistics the kit needs, in plain Python."""
import math
import statistics as st


def pearson(x, y):
    mx, my = st.mean(x), st.mean(y)
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy) if sx and sy else float("nan")


def ranks(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    out, i = [0.0] * len(v), 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(x, y):
    return pearson(ranks(x), ranks(y))


def ols(X, y):
    """Least squares by Gaussian elimination on the normal equations; X rows include the intercept."""
    k = len(X[0])
    A = [[sum(r[i] * r[j] for r in X) for j in range(k)] for i in range(k)]
    b = [sum(r[i] * t for r, t in zip(X, y)) for i in range(k)]
    for i in range(k):
        p = max(range(i, k), key=lambda r: abs(A[r][i]))
        A[i], A[p], b[i], b[p] = A[p], A[i], b[p], b[i]
        for r in range(i + 1, k):
            f = A[r][i] / A[i][i]
            A[r] = [a - f * c for a, c in zip(A[r], A[i])]
            b[r] -= f * b[i]
    coef = [0.0] * k
    for i in reversed(range(k)):
        coef[i] = (b[i] - sum(A[i][j] * coef[j] for j in range(i + 1, k))) / A[i][i]
    return coef


def predict(coef, x):
    return sum(c * v for c, v in zip(coef, x))


def r_squared(pred, y):
    my = st.mean(y)
    return 1 - sum((a - p) ** 2 for a, p in zip(y, pred)) / sum((a - my) ** 2 for a in y)


def rmse(errors):
    errors = list(errors)
    return math.sqrt(st.mean(e * e for e in errors))


def normal_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def normal_pdf(z):
    return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)


def expected_positive(mean, sd):
    """E[max(X, 0)] for X ~ Normal(mean, sd): what a holding is worth when the downside can be cut."""
    if sd <= 0:
        return max(mean, 0.0)
    z = mean / sd
    return mean * normal_cdf(z) + sd * normal_pdf(z)
