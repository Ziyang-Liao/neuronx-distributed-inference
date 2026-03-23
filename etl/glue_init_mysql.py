"""Glue Python Shell: Initialize MySQL with sample data"""
import sys
import pymysql

conn = pymysql.connect(
    host='<rds-endpoint>',
    user='admin', password='<your-password>', database='etl_source', port=3306
)
cur = conn.cursor()

cur.execute("DROP TABLE IF EXISTS user_data")
cur.execute("""CREATE TABLE user_data (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(256) NOT NULL,
    email VARCHAR(256) NOT NULL,
    phone VARCHAR(64),
    address VARCHAR(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_updated_at (updated_at)
)""")

data = [
    ('john_doe','john@example.com','555-0101','123 Main St, Springfield, IL'),
    ('jane_smith','jane@example.com','555-0102','456 Oak Ave, Portland, OR'),
    ('bob_wilson','bob@example.com','555-0103','789 Pine Rd, Austin, TX'),
    ('alice_chen','alice@example.com','555-0104','321 Elm St, Seattle, WA'),
    ('charlie_brown','charlie@example.com','555-0105','654 Maple Dr, Denver, CO'),
]
cur.executemany(
    "INSERT INTO user_data (username,email,phone,address) VALUES (%s,%s,%s,%s)", data
)
conn.commit()
print(f"Inserted {len(data)} rows")

cur.execute("SELECT COUNT(*) FROM user_data")
print(f"Total rows in MySQL: {cur.fetchone()[0]}")
cur.close()
conn.close()
