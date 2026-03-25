# MSK Serverless → MSK Connect → S3 Tables 实战操作指南

## 目录

- [1. 方案概述](#1-方案概述)
- [2. 前置条件](#2-前置条件)
- [3. 网络基础设施搭建](#3-网络基础设施搭建)
- [4. 创建 MSK Serverless 集群](#4-创建-msk-serverless-集群)
- [5. 创建 S3 Table Bucket 和 Namespace](#5-创建-s3-table-bucket-和-namespace)
- [6. 配置 IAM 权限](#6-配置-iam-权限)
- [7. 构建 Iceberg Kafka Connect 插件](#7-构建-iceberg-kafka-connect-插件)
- [8. 创建 MSK Connect Connector](#8-创建-msk-connect-connector)
- [9. 造数据验证](#9-造数据验证)
- [10. 踩坑记录与经验总结](#10-踩坑记录与经验总结)
- [11. 清理资源](#11-清理资源)

---

## 1. 方案概述

### 架构

```
造数据脚本 (Lambda/VPC)
        │
        ▼
┌─────────────────────────┐
│  MSK Serverless (IAM)   │  ← 私有子网，安全组仅允许 VPC 内部流量
│  Topic: demo-events     │
└─────────────────────────┘
        │
        ▼
┌─────────────────────────┐
│  MSK Connect             │  ← Iceberg Kafka Connect Sink
│  (Iceberg 1.7.1+)       │
└─────────────────────────┘
        │
        ▼ (REST Catalog + SigV4)
┌─────────────────────────┐
│  Amazon S3 Tables        │  ← Iceberg 表，自动 Compaction
│  Namespace: msk_demo     │
│  Table: events           │
└─────────────────────────┘
        │
        ▼
  Athena / EMR 查询
```

### 安全原则

- **所有服务部署在私有子网**
- **安全组入站规则仅允许 VPC CIDR，绝不开放 0.0.0.0/0**
- MSK 使用 IAM 认证 + SASL_SSL 加密传输
- 出站通过 NAT Gateway 访问 AWS 服务端点

---

## 2. 前置条件

- AWS 账号，具有 MSK、S3Tables、IAM、EC2、Lambda、ECR、CloudWatch 权限
- 一个 VPC，包含至少 2 个 AZ 的私有子网
- 私有子网需要通过 NAT Gateway 出网（MSK Connect 需要访问 S3 Tables REST API）
- Java 17+（用于构建 Iceberg Kafka Connect 插件）
- Docker（用于构建 Lambda 容器镜像）
- AWS CLI v2

---

## 3. 网络基础设施搭建

### 3.1 创建私有子网（如果没有）

```bash
# 在两个 AZ 各创建一个私有子网
aws ec2 create-subnet \
  --vpc-id vpc-XXXXX \
  --cidr-block 10.0.20.0/24 \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=msk-private-subnet-1a}]'

aws ec2 create-subnet \
  --vpc-id vpc-XXXXX \
  --cidr-block 10.0.21.0/24 \
  --availability-zone us-east-1b \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=msk-private-subnet-1b}]'
```

### 3.2 关联到 NAT Gateway 路由表

```bash
# 将私有子网关联到包含 NAT Gateway 路由的路由表
aws ec2 associate-route-table \
  --route-table-id rtb-XXXXX \
  --subnet-id subnet-XXXXX
```

### 3.3 创建安全组

```bash
aws ec2 create-security-group \
  --description "MSK and MSK Connect - VPC internal only" \
  --group-name msk-s3tables-sg \
  --vpc-id vpc-XXXXX \
  --tag-specifications 'ResourceType=security-group,Tags=[{Key=Name,Value=msk-s3tables-sg}]'

# 仅允许 VPC 内部流量（替换为你的 VPC CIDR）
aws ec2 authorize-security-group-ingress \
  --group-id sg-XXXXX \
  --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"10.0.0.0/16","Description":"VPC internal only"}]}]'

# 自引用规则
aws ec2 authorize-security-group-ingress \
  --group-id sg-XXXXX \
  --ip-permissions '[{"IpProtocol":"-1","UserIdGroupPairs":[{"GroupId":"sg-XXXXX","Description":"Self-referencing"}]}]'
```

> ⚠️ **安全底线**：入站规则绝不允许 0.0.0.0/0，仅限 VPC CIDR。

---

## 4. 创建 MSK Serverless 集群

```bash
aws kafka create-cluster-v2 \
  --cluster-name msk-s3tables-demo \
  --serverless '{
    "ClientAuthentication": {"Sasl": {"Iam": {"Enabled": true}}},
    "VpcConfigs": [{
      "SecurityGroupIds": ["sg-XXXXX"],
      "SubnetIds": ["subnet-1a-XXXXX", "subnet-1b-XXXXX"]
    }]
  }'
```

等待集群变为 ACTIVE（约 2-3 分钟）：

```bash
aws kafka describe-cluster-v2 --cluster-arn <CLUSTER_ARN> \
  --query 'ClusterInfo.State'
```

获取 Bootstrap Servers：

```bash
aws kafka get-bootstrap-brokers --cluster-arn <CLUSTER_ARN>
# 输出示例: boot-XXXXX.c1.kafka-serverless.us-east-1.amazonaws.com:9098
```

---

## 5. 创建 S3 Table Bucket 和 Namespace

### 5.1 创建 Table Bucket（如果没有）

```bash
aws s3tables create-table-bucket --name my-table-bucket --region us-east-1
```

### 5.2 创建 Namespace

```bash
aws s3tables create-namespace \
  --table-bucket-arn arn:aws:s3tables:us-east-1:<ACCOUNT_ID>:bucket/<BUCKET_NAME> \
  --namespace '["msk_demo"]'
```

---

## 6. 配置 IAM 权限

### 6.1 创建 MSK Connect Service Execution Role

```bash
aws iam create-role \
  --role-name msk-connect-s3tables-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "kafkaconnect.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
```

### 6.2 附加权限策略

```bash
aws iam put-role-policy \
  --role-name msk-connect-s3tables-role \
  --policy-name msk-connect-s3tables-policy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["kafka-cluster:*"],
        "Resource": [
          "arn:aws:kafka:us-east-1:<ACCOUNT_ID>:cluster/<CLUSTER_NAME>/*",
          "arn:aws:kafka:us-east-1:<ACCOUNT_ID>:topic/<CLUSTER_NAME>/*",
          "arn:aws:kafka:us-east-1:<ACCOUNT_ID>:group/<CLUSTER_NAME>/*",
          "arn:aws:kafka:us-east-1:<ACCOUNT_ID>:transactional-id/<CLUSTER_NAME>/*"
        ]
      },
      {
        "Effect": "Allow",
        "Action": ["s3tables:*"],
        "Resource": "*"
      },
      {
        "Effect": "Allow",
        "Action": ["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket","s3:GetBucketLocation"],
        "Resource": "*"
      },
      {
        "Effect": "Allow",
        "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
        "Resource": "*"
      }
    ]
  }'
```

> ⚠️ **关键**：`kafka-cluster:*` 的 Resource 必须包含正确的集群名称，否则会报 `Access denied`。

---

## 7. 构建 Iceberg Kafka Connect 插件

S3 Tables 需要 Iceberg 1.7.0+ 版本。需要从源码构建 Kafka Connect 插件。

### 7.1 克隆并构建

```bash
git clone --depth 1 --branch apache-iceberg-1.7.1 \
  https://github.com/apache/iceberg.git /tmp/iceberg-src

cd /tmp/iceberg-src
./gradlew :iceberg-kafka-connect:iceberg-kafka-connect-runtime:distZip \
  -x test -x integrationTest
```

构建产物位于：
```
kafka-connect/kafka-connect-runtime/build/distributions/iceberg-kafka-connect-runtime-*.zip
```

### 7.2 上传到 S3

```bash
aws s3 cp /tmp/iceberg-src/kafka-connect/kafka-connect-runtime/build/distributions/*.zip \
  s3://<YOUR_BUCKET>/msk-plugins/iceberg-kafka-connect-runtime.zip
```

### 7.3 创建 MSK Connect Custom Plugin

```bash
aws kafkaconnect create-custom-plugin \
  --name iceberg-kafka-connect-plugin \
  --content-type ZIP \
  --location '{
    "s3Location": {
      "bucketArn": "arn:aws:s3:::<YOUR_BUCKET>",
      "fileKey": "msk-plugins/iceberg-kafka-connect-runtime.zip"
    }
  }'
```

等待 Plugin 变为 ACTIVE（约 1-2 分钟）。

---

## 8. 创建 MSK Connect Connector

### 8.1 预创建 Kafka Topics

MSK Serverless 不会自动创建 topic。需要在 VPC 内通过 Kafka AdminClient 提前创建：

- `demo-events` — 数据 topic（3 partitions）
- `control-iceberg` — Iceberg Connector 内部协调 topic（3 partitions）

可以通过 Lambda（部署在 MSK 的私有子网中）来创建 topic，参见第 9 节。

### 8.2 创建 CloudWatch Log Group

```bash
aws logs create-log-group --log-group-name /msk-connect/iceberg-s3tables-sink
```

### 8.3 创建 Connector

```bash
aws kafkaconnect create-connector \
  --connector-name "iceberg-s3tables-sink" \
  --capacity '{"provisionedCapacity":{"mcuCount":1,"workerCount":1}}' \
  --connector-configuration '{
    "connector.class": "org.apache.iceberg.connect.IcebergSinkConnector",
    "tasks.max": "2",
    "topics": "demo-events",

    "key.converter": "org.apache.kafka.connect.storage.StringConverter",
    "value.converter": "org.apache.kafka.connect.json.JsonConverter",
    "value.converter.schemas.enable": "false",

    "iceberg.catalog.type": "rest",
    "iceberg.catalog.uri": "https://s3tables.us-east-1.amazonaws.com/iceberg",
    "iceberg.catalog.warehouse": "arn:aws:s3tables:us-east-1:<ACCOUNT_ID>:bucket/<BUCKET_NAME>",
    "iceberg.catalog.rest.sigv4-enabled": "true",
    "iceberg.catalog.rest.signing-name": "s3tables",
    "iceberg.catalog.rest.signing-region": "us-east-1",
    "iceberg.catalog.client.region": "us-east-1",
    "iceberg.catalog.s3.region": "us-east-1",

    "iceberg.tables": "msk_demo.events",
    "iceberg.tables.auto-create-enabled": "true",
    "iceberg.tables.evolve-schema-enabled": "true",
    "iceberg.control.commit.interval-ms": "60000",
    "iceberg.control.commit.timeout-ms": "300000"
  }' \
  --kafka-cluster '{
    "apacheKafkaCluster": {
      "bootstrapServers": "<BOOTSTRAP_SERVERS>:9098",
      "vpc": {
        "securityGroups": ["sg-XXXXX"],
        "subnets": ["subnet-1a-XXXXX", "subnet-1b-XXXXX"]
      }
    }
  }' \
  --kafka-cluster-client-authentication '{"authenticationType":"IAM"}' \
  --kafka-cluster-encryption-in-transit '{"encryptionType":"TLS"}' \
  --kafka-connect-version "2.7.1" \
  --plugins '[{
    "customPlugin": {
      "customPluginArn": "<PLUGIN_ARN>",
      "revision": 1
    }
  }]' \
  --service-execution-role-arn "arn:aws:iam::<ACCOUNT_ID>:role/msk-connect-s3tables-role" \
  --log-delivery '{
    "workerLogDelivery": {
      "cloudWatchLogs": {
        "enabled": true,
        "logGroup": "/msk-connect/iceberg-s3tables-sink"
      }
    }
  }'
```

等待 Connector 变为 RUNNING（约 10-15 分钟）：

```bash
aws kafkaconnect describe-connector --connector-arn <CONNECTOR_ARN> \
  --query 'connectorState'
```

### 8.4 关键配置说明

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `key.converter` | `StringConverter` | ⚠️ 不能用 JsonConverter，因为 key 是纯字符串 |
| `iceberg.catalog.type` | `rest` | S3 Tables 使用 REST Catalog |
| `iceberg.catalog.rest.sigv4-enabled` | `true` | 启用 AWS SigV4 签名 |
| `iceberg.catalog.rest.signing-name` | `s3tables` | 签名服务名 |
| `iceberg.catalog.client.region` | `us-east-1` | ⚠️ 必须显式设置，MSK Connect 无 EC2 metadata |
| `iceberg.catalog.s3.region` | `us-east-1` | ⚠️ 同上，S3FileIO 需要 region |
| `iceberg.tables.auto-create-enabled` | `true` | 自动在 S3 Tables 中创建 Iceberg 表 |

---

## 9. 造数据验证

### 9.1 创建 Lambda 造数据函数

由于 MSK Serverless 在私有子网中，需要通过 VPC 内的 Lambda 来发送数据。

#### Dockerfile

```dockerfile
FROM public.ecr.aws/lambda/python:3.11
RUN pip install confluent-kafka==2.3.0 aws-msk-iam-sasl-signer-python
COPY lambda_function.py ${LAMBDA_TASK_ROOT}/
CMD ["lambda_function.handler"]
```

#### lambda_function.py

```python
import json, random, uuid
from datetime import datetime, timezone
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic
from aws_msk_iam_sasl_signer import MSKAuthTokenProvider

BOOTSTRAP = "<BOOTSTRAP_SERVERS>:9098"
REGION = "us-east-1"

def oauth_cb(config_str):
    token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(REGION)
    return token, expiry_ms / 1000

def get_admin():
    admin = AdminClient({
        "bootstrap.servers": BOOTSTRAP,
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "OAUTHBEARER",
        "oauth_cb": oauth_cb,
    })
    for _ in range(10):
        admin.poll(1)
    return admin

CATEGORIES = ["electronics", "books", "clothing", "food", "toys"]
CITIES = ["Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Chengdu"]

def handler(event, context):
    action = event.get("action", "produce")

    if action == "admin":
        admin = get_admin()
        for t in event.get("delete_topics", []):
            try:
                fs = admin.delete_topics([t])
                for topic, f in fs.items():
                    f.result()
            except Exception as e:
                print(f"Delete {t}: {e}")
        for t in event.get("create_topics", []):
            try:
                fs = admin.create_topics([NewTopic(t, num_partitions=3, replication_factor=3)])
                for topic, f in fs.items():
                    f.result()
                    print(f"Created {topic}")
            except Exception as e:
                print(f"Create {t}: {e}")
        topics = admin.list_topics(timeout=30).topics
        return {"statusCode": 200, "body": f"Topics: {list(topics.keys())}"}

    # Produce
    topic = event.get("topic", "demo-events")
    count = event.get("count", 100)
    conf = {
        "bootstrap.servers": BOOTSTRAP,
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "OAUTHBEARER",
        "oauth_cb": oauth_cb,
        "client.id": "lambda-producer",
        "message.timeout.ms": 60000,
    }
    producer = Producer(conf)
    for _ in range(10):
        producer.poll(1)

    delivered = [0]
    def dcb(err, msg):
        if not err:
            delivered[0] += 1

    for i in range(count):
        msg = {
            "id": str(uuid.uuid4()),
            "event_type": random.choice(["purchase", "view", "click", "refund"]),
            "category": random.choice(CATEGORIES),
            "city": random.choice(CITIES),
            "amount": round(random.uniform(10, 5000), 2),
            "quantity": random.randint(1, 10),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        producer.produce(topic, key=msg["id"], value=json.dumps(msg), callback=dcb)
        if (i + 1) % 10 == 0:
            producer.poll(0)

    producer.flush(timeout=60)
    return {"statusCode": 200, "body": f"Delivered: {delivered[0]}/{count}"}
```

#### 构建并部署

```bash
# 构建 Docker 镜像
docker build -t msk-producer-lambda .

# 推送到 ECR
aws ecr create-repository --repository-name msk-producer-lambda
aws ecr get-login-password | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
docker tag msk-producer-lambda:latest <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/msk-producer-lambda:latest
docker push <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/msk-producer-lambda:latest

# 创建 Lambda（部署在 MSK 的私有子网中）
aws lambda create-function \
  --function-name msk-demo-producer \
  --package-type Image \
  --code ImageUri=<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/msk-producer-lambda:latest \
  --role "arn:aws:iam::<ACCOUNT_ID>:role/lambda-msk-producer-role" \
  --timeout 120 --memory-size 256 \
  --vpc-config SubnetIds=subnet-1a-XXXXX,subnet-1b-XXXXX,SecurityGroupIds=sg-XXXXX
```

> ⚠️ Lambda 的 IAM Role 需要 `kafka-cluster:*`、`ec2:CreateNetworkInterface` 等权限。

### 9.2 创建 Topics

```bash
aws lambda invoke --function-name msk-demo-producer \
  --payload '{"action":"admin","create_topics":["demo-events","control-iceberg"]}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/out.json && cat /tmp/out.json
```

### 9.3 发送测试数据

```bash
aws lambda invoke --function-name msk-demo-producer \
  --payload '{"count":100}' \
  --cli-binary-format raw-in-base64-out \
  --cli-read-timeout 120 \
  /tmp/out.json && cat /tmp/out.json
# 期望输出: {"statusCode": 200, "body": "Delivered: 100/100"}
```

### 9.4 验证 S3 Tables

等待 60-90 秒（Connector commit interval），然后检查：

```bash
# 检查表是否创建
aws s3tables list-tables \
  --table-bucket-arn arn:aws:s3tables:us-east-1:<ACCOUNT_ID>:bucket/<BUCKET_NAME> \
  --namespace msk_demo

# 查看表详情
aws s3tables get-table \
  --table-bucket-arn arn:aws:s3tables:us-east-1:<ACCOUNT_ID>:bucket/<BUCKET_NAME> \
  --namespace msk_demo --name events

# 查看自动维护状态
aws s3tables get-table-maintenance-job-status \
  --table-bucket-arn arn:aws:s3tables:us-east-1:<ACCOUNT_ID>:bucket/<BUCKET_NAME> \
  --namespace msk_demo --name events
```

### 9.5 用 Athena 查询

```sql
SELECT * FROM msk_demo.events LIMIT 10;
```

---

## 10. 踩坑记录与经验总结

### 10.1 key.converter 必须用 StringConverter

**现象**：Connector 启动后立即 FAILED，日志报 `Converting byte[] to Kafka Connect data failed due to serialization error`。

**原因**：Producer 发送的 key 是纯字符串 UUID（如 `2b39ede0-9bd0-4021-ab64-a408373f3f6a`），`JsonConverter` 无法将其解析为 JSON。

**解决**：`key.converter` 设为 `org.apache.kafka.connect.storage.StringConverter`。

### 10.2 必须显式设置 AWS Region

**现象**：Connector 报 `Unable to load region from any of the providers in the chain`。

**原因**：MSK Connect Worker 运行在 Fargate 容器中，没有 EC2 Instance Metadata，AWS SDK 无法自动获取 Region。

**解决**：在 Connector 配置中添加：
```
iceberg.catalog.client.region=us-east-1
iceberg.catalog.s3.region=us-east-1
```

### 10.3 IAM 策略 Resource 必须匹配集群名

**现象**：Connector 报 `SaslAuthenticationException: Access denied`。

**原因**：MSK Serverless IAM 认证要求 Resource ARN 中包含正确的集群名称。

**解决**：IAM 策略 Resource 格式为 `arn:aws:kafka:<region>:<account>:cluster/<cluster-name>/*`。

### 10.4 必须预创建 control-iceberg Topic

**现象**：Connector 启动后日志持续报 `UNKNOWN_TOPIC_OR_PARTITION` for `control-iceberg`。

**原因**：Iceberg Kafka Connect 需要 `control-iceberg` topic 来协调 commit，MSK Serverless 不自动创建 topic。

**解决**：通过 AdminClient 提前创建 `control-iceberg` topic（3 partitions, replication-factor 3）。

### 10.5 MSK Serverless Partition 配额限制

**现象**：Connector 报 `Quota exceeded for maximum number of partitions`。

**原因**：MSK Serverless 默认限制 120 个 partition。每个 MSK Connect Connector 会创建 3 个内部 topic（configs 1分区 + offsets 25分区 + status 5分区 = 31 分区），反复创建/删除 Connector 会快速耗尽配额。

**解决**：
- 避免频繁创建/删除 Connector
- 如果配额耗尽，创建新的 MSK Serverless 集群
- 或通过 AWS Support 申请提高配额

### 10.6 kafka-python-ng 不支持 MSK IAM 认证

**现象**：使用 `kafka-python-ng` 的 Producer 连接 MSK Serverless 超时。

**原因**：`kafka-python-ng` 的 OAUTHBEARER 实现与 MSK IAM 的 SASL 握手协议不完全兼容。

**解决**：使用 `confluent-kafka` Python 库，通过 `oauth_cb` 回调提供 MSK IAM token。注意需要在 Docker 容器中构建（Lambda 需要 container image 方式部署）。

### 10.7 MSK Connect 创建时间长

MSK Connect 创建 Connector 通常需要 **10-15 分钟**，这是正常行为。底层需要启动 Fargate 容器、下载插件、建立连接。

---

## 11. 清理资源

```bash
# 1. 删除 MSK Connect Connector
aws kafkaconnect delete-connector --connector-arn <CONNECTOR_ARN>

# 2. 删除 MSK Connect Custom Plugin
aws kafkaconnect delete-custom-plugin --custom-plugin-arn <PLUGIN_ARN>

# 3. 删除 MSK Serverless 集群
aws kafka delete-cluster --cluster-arn <CLUSTER_ARN>

# 4. 删除 Lambda
aws lambda delete-function --function-name msk-demo-producer

# 5. 删除 ECR 仓库
aws ecr delete-repository --repository-name msk-producer-lambda --force

# 6. 删除 IAM Role
aws iam delete-role-policy --role-name msk-connect-s3tables-role --policy-name msk-connect-s3tables-policy
aws iam delete-role --role-name msk-connect-s3tables-role

# 7. 删除安全组（确认无依赖后）
aws ec2 delete-security-group --group-id sg-XXXXX

# 8. 删除私有子网（如果是专门创建的）
aws ec2 delete-subnet --subnet-id subnet-XXXXX

# 9. 删除 CloudWatch Log Group
aws logs delete-log-group --log-group-name /msk-connect/iceberg-s3tables-sink

# 10. 删除 S3 Tables 中的表（可选）
aws s3tables delete-table \
  --table-bucket-arn arn:aws:s3tables:us-east-1:<ACCOUNT_ID>:bucket/<BUCKET_NAME> \
  --namespace msk_demo --name events
```

---

## 参考资源

- [Apache Iceberg Kafka Connect 文档](https://iceberg.apache.org/docs/latest/kafka-connect/)
- [Amazon S3 Tables 文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables.html)
- [Amazon MSK Connect 文档](https://docs.aws.amazon.com/msk/latest/developerguide/msk-connect.html)
- [S3 Tables REST Catalog 集成](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-open-source.html)
