"""StaticPricing: the single source of prices, and the port's two guarantees.

The port forbids raising and forbids returning zero for an unknown input —
zero would read as "free" and silently hide waste from the savings total.
"""
import logging
from decimal import Decimal

import pytest

from finops_sentinel.adapters.aws.pricing import (
    EBS_GB_MONTH,
    EC2_HOURLY,
    PRICED_REGION,
    StaticPricing,
)
from finops_sentinel.ports.pricing import Pricing


@pytest.fixture
def pricing():
    return StaticPricing()


def test_implements_the_port(pricing):
    assert isinstance(pricing, Pricing)


def test_ebs_volume_priced_per_gb_by_type(pricing):
    # gp3 at $0.08/GB-mo * 100 GB
    assert pricing.ebs_volume_monthly("gp3", 100, PRICED_REGION) == Decimal("8.00")
    # gp2 at $0.10/GB-mo * 50 GB
    assert pricing.ebs_volume_monthly("gp2", 50, PRICED_REGION) == Decimal("5.00")


def test_snapshot_priced_per_gb(pricing):
    assert pricing.ebs_snapshot_monthly(10, PRICED_REGION) == Decimal("0.50")


def test_elastic_ip_is_hourly_rate_times_730(pricing):
    # $0.005/hr * 730 hrs
    assert pricing.elastic_ip_monthly(PRICED_REGION) == Decimal("3.65")


def test_ec2_instance_is_hourly_rate_times_730(pricing):
    # m5.large at $0.096/hr * 730 hrs
    assert pricing.ec2_instance_monthly("m5.large", PRICED_REGION) == Decimal("70.08")


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.ebs_volume_monthly("nvme9.ultra", 100, PRICED_REGION),
        lambda p: p.ec2_instance_monthly("zz.42xlarge", PRICED_REGION),
        lambda p: p.ec2_instance_monthly("", PRICED_REGION),
    ],
)
def test_unknown_sku_returns_a_nonzero_estimate(pricing, call):
    """Never zero: a $0 finding looks free and drops out of the savings total."""
    assert call(pricing) > Decimal(0)


def test_unknown_ebs_type_defaults_to_the_priciest_common_type(pricing):
    """Erring high keeps unknown volumes from looking harmless."""
    unknown = pricing.ebs_volume_monthly("nvme9.ultra", 100, PRICED_REGION)
    assert unknown == pricing.ebs_volume_monthly("gp2", 100, PRICED_REGION)


def test_zero_sized_volume_is_free_but_does_not_raise(pricing):
    assert pricing.ebs_volume_monthly("gp3", 0, PRICED_REGION) == Decimal(0)


def test_costs_are_rounded_to_cents(pricing):
    # t3.micro: 0.0104 * 730 = 7.592 -> 7.59
    assert pricing.ec2_instance_monthly("t3.micro", PRICED_REGION) == Decimal("7.59")
    assert pricing.ec2_instance_monthly("t3.micro", PRICED_REGION).as_tuple().exponent == -2


def test_offregion_pricing_warns_once_per_region(pricing, caplog):
    """Static rates are us-east-1; silently under-reporting elsewhere is a trap."""
    with caplog.at_level(logging.WARNING):
        pricing.elastic_ip_monthly("eu-west-1")
        pricing.elastic_ip_monthly("eu-west-1")
        pricing.elastic_ip_monthly("ap-south-1")

    warnings = [r for r in caplog.records if "list prices" in r.message]
    assert len(warnings) == 2  # deduplicated per region, not per call


def test_priced_region_does_not_warn(pricing, caplog):
    with caplog.at_level(logging.WARNING):
        pricing.elastic_ip_monthly(PRICED_REGION)

    assert caplog.records == []


def test_price_tables_are_positive_decimals():
    """Guards against a float or a typo'd negative sneaking into the tables."""
    for table in (EBS_GB_MONTH, EC2_HOURLY):
        for sku, rate in table.items():
            assert isinstance(rate, Decimal), f"{sku} must be Decimal, not float"
            assert rate > 0, f"{sku} must be positive"
