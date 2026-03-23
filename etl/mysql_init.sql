-- MySQL: Create sample source table
-- Run this on etl-mysql.cuf6dqeperny.us-east-1.rds.amazonaws.com:3306/etl_source

CREATE TABLE IF NOT EXISTS user_data (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(256) NOT NULL,
    email VARCHAR(256) NOT NULL,
    phone VARCHAR(64),
    address VARCHAR(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_updated_at (updated_at)
);

-- Sample data
INSERT INTO user_data (username, email, phone, address) VALUES
('john_doe', 'john@example.com', '555-0101', '123 Main St, Springfield, IL'),
('jane_smith', 'jane@example.com', '555-0102', '456 Oak Ave, Portland, OR'),
('bob_wilson', 'bob@example.com', '555-0103', '789 Pine Rd, Austin, TX'),
('alice_chen', 'alice@example.com', '555-0104', '321 Elm St, Seattle, WA'),
('charlie_brown', 'charlie@example.com', '555-0105', '654 Maple Dr, Denver, CO');
