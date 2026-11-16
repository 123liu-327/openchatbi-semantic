-- Statically converted from chatbi_mvp:database/init.sql; SQLite demo only.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dim_customers (
    customer_id INT NOT NULL,
    customer_name VARCHAR(100) NOT NULL,
    customer_type VARCHAR(50) NOT NULL,
    industry VARCHAR(50) NOT NULL,
    country VARCHAR(50) NOT NULL,
    region VARCHAR(50) NOT NULL,
    PRIMARY KEY (customer_id)
);

CREATE TABLE IF NOT EXISTS dim_products (
    product_id INT NOT NULL,
    product_name VARCHAR(100) NOT NULL,
    product_line VARCHAR(50) NOT NULL,
    category VARCHAR(50) NOT NULL,
    tech_route VARCHAR(50) NOT NULL,
    standard_cost DECIMAL(10,2) NOT NULL,
    material_cost DECIMAL(10,2) NOT NULL,
    labor_cost DECIMAL(10,2) NOT NULL,
    PRIMARY KEY (product_id)
);

CREATE TABLE IF NOT EXISTS exchange_rates (
    rate_date DATE NOT NULL,
    currency VARCHAR(10) NOT NULL,
    rate_to_cny DECIMAL(10,4) NOT NULL,
    PRIMARY KEY (rate_date, currency)
);

CREATE TABLE IF NOT EXISTS finance_expenses (
    expense_id BIGINT NOT NULL,
    expense_date DATE NOT NULL,
    department VARCHAR(50) NOT NULL,
    rd_expense DECIMAL(12,2) NOT NULL,
    selling_expense DECIMAL(12,2) NOT NULL,
    admin_expense DECIMAL(12,2) NOT NULL,
    finance_expense DECIMAL(12,2) NOT NULL,
    marketing_expense DECIMAL(12,2) NOT NULL,
    logistics_expense DECIMAL(12,2) NOT NULL,
    warranty_expense DECIMAL(12,2) NOT NULL,
    PRIMARY KEY (expense_id)
);

CREATE TABLE IF NOT EXISTS sales_orders (
    order_id BIGINT NOT NULL,
    order_no VARCHAR(50) NOT NULL,
    customer_id INT NOT NULL,
    product_id INT NOT NULL,
    region VARCHAR(50) NOT NULL,
    order_date DATE NOT NULL,
    order_status VARCHAR(20) NOT NULL,
    quantity DECIMAL(10,2) NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    discount_amount DECIMAL(10,2) NOT NULL,
    gross_amount DECIMAL(12,2) NOT NULL,
    net_amount DECIMAL(12,2) NOT NULL,
    currency VARCHAR(10) NOT NULL,
    PRIMARY KEY (order_id),
    FOREIGN KEY (customer_id) REFERENCES dim_customers (customer_id),
    FOREIGN KEY (product_id) REFERENCES dim_products (product_id)
);

INSERT OR REPLACE INTO dim_customers (customer_id, customer_name, customer_type, industry, country, region) VALUES
(1, '华东新能源汽车集团', 'OEM整车厂', '交通', '中国', '华东'),
    (2, '北美绿色出行公司', 'OEM整车厂', '交通', '美国', '北美'),
    (3, '莱茵储能集成有限公司', '储能集成商', '能源', 'Germany', '欧洲'),
    (4, '南方智慧电网集团', '电网集团', '能源', '中国', '华南'),
    (5, '长江工商业能源公司', '工商业用户', '工业', '中国', '华中');

INSERT OR REPLACE INTO dim_products (product_id, product_name, product_line, category, tech_route,
    standard_cost, material_cost, labor_cost) VALUES
(101, '麒麟高能电池包', '动力电池-乘用车', '高能量密度型', '三元锂', 82000.00, 65000.00, 12000.00),
    (102, '神行超充电池包', '动力电池-乘用车', '超快充型', '磷酸铁锂', 70000.00, 55000.00, 10000.00),
    (103, '商用车标准电池箱', '动力电池-商用车', '商用车标准型', '磷酸铁锂', 96000.00, 76000.00, 14000.00),
    (104, '电网级储能系统', '储能系统-电网级', '电网级储能型', '磷酸铁锂', 61000.00, 47000.00, 9000.00),
    (105, '工商业储能柜', '储能系统-工商业', '工商业储能型', '钠离子', 54000.00, 40000.00, 8500.00);

INSERT OR REPLACE INTO exchange_rates (rate_date, currency, rate_to_cny) VALUES
('2026-05-15', 'USD', 7.1800),
    ('2026-06-20', 'EUR', 7.8200),
    ('2026-07-10', 'CNY', 1.0000),
    ('2026-08-18', 'USD', 7.1600),
    ('2026-09-25', 'EUR', 7.7900);

INSERT OR REPLACE INTO finance_expenses (expense_id, expense_date, department,
    rd_expense, selling_expense, admin_expense, finance_expense,
    marketing_expense, logistics_expense, warranty_expense) VALUES
(5001, '2026-05-31', '集团总部', 3200000.00, 1450000.00, 820000.00, 310000.00, 420000.00, 510000.00, 180000.00),
    (5002, '2026-06-30', '集团总部', 3500000.00, 1520000.00, 850000.00, 295000.00, 460000.00, 525000.00, 190000.00),
    (5003, '2026-07-31', '集团总部', 3800000.00, 1610000.00, 880000.00, 330000.00, 500000.00, 550000.00, 205000.00),
    (5004, '2026-08-31', '集团总部', 4100000.00, 1740000.00, 910000.00, 345000.00, 560000.00, 585000.00, 215000.00),
    (5005, '2026-09-30', '集团总部', 4300000.00, 1810000.00, 930000.00, 360000.00, 590000.00, 610000.00, 225000.00);

INSERT OR REPLACE INTO sales_orders (order_id, order_no, customer_id, product_id, region, order_date,
    order_status, quantity, unit_price, discount_amount,
    gross_amount, net_amount, currency) VALUES
(1001, 'SO-20260515-001', 2, 101, '北美', '2026-05-15', 'completed', 10.00, 150000.00,     0.00, 1695000.00, 1500000.00, 'USD'),
    (1002, 'SO-20260515-002', 2, 102, '北美', '2026-05-15', 'completed',  8.00, 170000.00, 40000.00, 1491600.00, 1320000.00, 'USD'),
    (1003, 'SO-20260620-001', 3, 104, '欧洲', '2026-06-20', 'completed',  6.00, 210000.00, 30000.00, 1389900.00, 1230000.00, 'EUR'),
    (1004, 'SO-20260620-002', 3, 105, '欧洲', '2026-06-20', 'cancelled', 12.00, 125000.00, 50000.00, 1638500.00, 1450000.00, 'EUR'),
    (1005, 'SO-20260710-001', 4, 104, '华南', '2026-07-10', 'completed', 25.00,  95000.00, 75000.00, 2599000.00, 2300000.00, 'CNY'),
    (1006, 'SO-20260710-002', 5, 105, '华中', '2026-07-10', 'completed', 18.00, 110000.00, 40000.00, 2192200.00, 1940000.00, 'CNY'),
    (1007, 'SO-20260818-001', 2, 103, '北美', '2026-08-18', 'completed',  7.00, 220000.00, 40000.00, 1695000.00, 1500000.00, 'USD'),
    (1008, 'SO-20260818-002', 2, 102, '北美', '2026-08-18', 'pending',   10.00, 130000.00, 30000.00, 1435100.00, 1270000.00, 'USD'),
    (1009, 'SO-20260925-001', 3, 104, '欧洲', '2026-09-25', 'completed', 15.00,  98000.00, 50000.00, 1604600.00, 1420000.00, 'EUR'),
    (1010, 'SO-20260925-002', 3, 101, '欧洲', '2026-09-25', 'completed',  9.00, 160000.00, 40000.00, 1582000.00, 1400000.00, 'EUR');
