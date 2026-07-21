import boto3
import time

def seed_resources():
    print("Seeding LocalStack with test resources...")
    client = boto3.client(
        "ec2", 
        endpoint_url="http://localhost:4566", 
        region_name="us-east-1", 
        aws_access_key_id="test", 
        aws_secret_access_key="test"
    )
    
    # 1. Create unattached EBS Volume
    vol = client.create_volume(Size=10, AvailabilityZone="us-east-1a")
    print(f"✅ Created EBS Volume: {vol['VolumeId']}")
    
    # 2. Create orphaned Elastic IP (EIP)
    eip = client.allocate_address(Domain='vpc')
    print(f"✅ Created Elastic IP: {eip['AllocationId']} ({eip['PublicIp']})")
    
    # 3. Create stopped EC2 Instance
    instances = client.run_instances(
        ImageId='ami-0c55b159cbfafe1f0', # Dummy AMI for LocalStack
        MinCount=1,
        MaxCount=1,
        InstanceType='t2.micro'
    )
    instance_id = instances['Instances'][0]['InstanceId']
    print(f"✅ Created EC2 Instance: {instance_id}")
    
    # Stop the instance so it gets flagged by the scanner
    print(f"Stopping instance {instance_id}...")
    client.stop_instances(InstanceIds=[instance_id])
    
    # Wait for instance to actually stop
    waiter = client.get_waiter('instance_stopped')
    waiter.wait(InstanceIds=[instance_id], WaiterConfig={'Delay': 2, 'MaxAttempts': 10})
    print(f"✅ EC2 Instance {instance_id} is now stopped.")

if __name__ == "__main__":
    seed_resources()
    print("\nAll resources seeded! Run: .venv/bin/sentinel scan")
