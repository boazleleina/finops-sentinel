from statistics import fmean


def mean_or_zero(values: list[float]) -> float:
    """Mean of a CloudWatch series, 0.0 for an empty one.

    Not bare statistics.fmean, which raises on an empty list — and an empty
    series is a legitimate answer from CloudWatch (a metric the instance never
    published, a gap in retention). Shared here because the idle scanners each
    grew their own identical copy.

    Callers must still gate the DECISION on *_min_datapoints: 0.0 for "no
    data" is fine for a secondary signal like network throughput, but a
    verdict needs a series long enough to mean something.
    """
    return fmean(values) if values else 0.0
