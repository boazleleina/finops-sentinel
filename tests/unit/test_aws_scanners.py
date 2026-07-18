from decimal import Decimal
from finops_sentinel.adapters.aws.scanners.ebs import UnattachedEBSScanner
from finops_sentinel.adapters.aws.scanners.eip import OrphanedEIPScanner
from finops_sentinel.adapters.aws.scanners.ec2 import StoppedEC2Scanner
from finops_sentinel.adapters.aws.gateway import Boto3Gateway
from finops_sentinel.domain.models import ResourceType

def test_ebs_scanner(mock_aws_env):
    """Test EBS scanner discover and evaluate passes."""
    ec2 = mock_aws_env
    
    # Create an unattached GP2 volume
    vol_gp2 = ec2.create_volume(AvailabilityZone="us-east-1a", Size=50, VolumeType="gp2")
    # Create an unattached GP3 volume
    vol_gp3 = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3")
    
    gateway = Boto3Gateway(region="us-east-1")
    scanner = UnattachedEBSScanner(region="us-east-1", gp2_price=0.10, gp3_price=0.08)
    
    # Pass 1: Discover
    resources = scanner.discover(gateway)
    assert len(resources) == 2
    
    res_gp2 = next(r[0] for r in resources if r[0].resource_id == vol_gp2["VolumeId"])
    res_gp3 = next(r[0] for r in resources if r[0].resource_id == vol_gp3["VolumeId"])
    
    assert res_gp2.resource_type == ResourceType.EBS_VOLUME
    
    # Pass 2: Evaluate
    findings = scanner.evaluate(resources)
    assert len(findings) == 2
    
    finding_gp2 = next(f for f in findings if f.resource_ref == res_gp2.id)
    finding_gp3 = next(f for f in findings if f.resource_ref == res_gp3.id)
    
    # GP2: 50 * 0.10 = 5.00
    assert finding_gp2.est_monthly_cost_usd == Decimal("5.00")
    # GP3: 100 * 0.08 = 8.00
    assert finding_gp3.est_monthly_cost_usd == Decimal("8.00")

def test_eip_scanner(mock_aws_env):
    """Test EIP scanner discover and evaluate passes."""
    ec2 = mock_aws_env
    
    # Create unattached EIP
    eip = ec2.allocate_address(Domain="vpc")
    
    # Create attached EIP (need VPC, Subnet, Instance to attach properly in moto)
    # We will just test the unattached one for now.
    
    gateway = Boto3Gateway(region="us-east-1")
    scanner = OrphanedEIPScanner(region="us-east-1", eip_price=3.65)
    
    resources = scanner.discover(gateway)
    assert len(resources) == 1
    
    res_eip = resources[0][0]
    assert res_eip.resource_id == eip["AllocationId"]
    assert res_eip.resource_type == ResourceType.ELASTIC_IP
    
    findings = scanner.evaluate(resources)
    assert len(findings) == 1
    assert findings[0].est_monthly_cost_usd == Decimal("3.65")
    assert findings[0].resource_ref == res_eip.id

def test_ec2_scanner(mock_aws_env):
    """Test EC2 scanner discover and evaluate passes."""
    ec2 = mock_aws_env
    
    # Create a running instance (not stopped, should be ignored by scanner logic usually, 
    # but discover_instances only looks for stopped in gateway!)
    
    # Moto setup to run instances
    res = ec2.run_instances(ImageId="ami-12c6146b", MinCount=1, MaxCount=1, InstanceType="t2.micro")
    instance_id = res["Instances"][0]["InstanceId"]
    
    # Stop the instance so it's picked up
    ec2.stop_instances(InstanceIds=[instance_id])
    
    gateway = Boto3Gateway(region="us-east-1")
    scanner = StoppedEC2Scanner(region="us-east-1")
    
    resources = scanner.discover(gateway)
    assert len(resources) == 1
    
    res_ec2 = resources[0][0]
    assert res_ec2.resource_id == instance_id
    assert res_ec2.resource_type == ResourceType.EC2_INSTANCE
    
    findings = scanner.evaluate(resources)
    assert len(findings) == 1
    assert findings[0].est_monthly_cost_usd == Decimal("5.00") # static cost in ec2.py
