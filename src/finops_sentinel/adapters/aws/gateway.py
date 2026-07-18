import boto3
from typing import List, Dict, Any, Optional
from finops_sentinel.ports.cloud import CloudGateway

class Boto3Gateway(CloudGateway):
    """
    AWS Adapter implementing the CloudGateway port using boto3.
    """
    def __init__(self, region: str, endpoint_url: Optional[str] = None, aws_access_key_id: Optional[str] = None, aws_secret_access_key: Optional[str] = None):
        self.client = boto3.client(
            'ec2', 
            region_name=region, 
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key
        )

    def describe_ebs_volumes(self) -> List[Dict[str, Any]]:
        volumes = []
        paginator = self.client.get_paginator('describe_volumes')
        page_iterator = paginator.paginate(Filters=[{'Name': 'status', 'Values': ['available']}])
        for page in page_iterator:
            volumes.extend(page.get('Volumes', []))
        return volumes

    def describe_elastic_ips(self) -> List[Dict[str, Any]]:
        response = self.client.describe_addresses()
        return response.get('Addresses', [])

    def describe_ec2_instances(self) -> List[Dict[str, Any]]:
        instances = []
        paginator = self.client.get_paginator('describe_instances')
        page_iterator = paginator.paginate(Filters=[{'Name': 'instance-state-name', 'Values': ['stopped']}])
        for page in page_iterator:
            for reservation in page.get('Reservations', []):
                instances.extend(reservation.get('Instances', []))
        return instances
