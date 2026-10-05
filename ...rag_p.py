CREATE TABLE billing_config (                      
    key   VARCHAR(50)  PRIMARY KEY,        
    value VARCHAR(100) NOT NULL
)
--------------------------------------------------------------------------------
CREATE TABLE customers (
    customer_id VARCHAR(50)  PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    company     VARCHAR(200),
    email       VARCHAR(200)
)
--------------------------------------------------------------------------------
CREATE TABLE invoice_lines (
    id               SERIAL        PRIMARY KEY,
    order_id         VARCHAR(50)   NOT NULL REFERENCES orders(order_id),
    item_code        VARCHAR(50)   NOT NULL REFERENCES items(item_code),
    qty_billed       INTEGER       NOT NULL,
    unit_price_billed DECIMAL(10,2) NOT NULL,
    line_total       DECIMAL(10,2) NOT NULL
)
--------------------------------------------------------------------------------
CREATE TABLE items (
    sku               VARCHAR(50)   PRIMARY KEY,
    item_code         VARCHAR(50)   UNIQUE NOT NULL,
    name              VARCHAR(200)  NOT NULL,
    baseprice         DECIMAL(10,2) NOT NULL,
    sellingprice      DECIMAL(10,2) NOT NULL,
    availablequantity INTEGER       NOT NULL DEFAULT 0
)
--------------------------------------------------------------------------------
CREATE TABLE order_fulfilled (
    id        SERIAL      PRIMARY KEY,
    order_id  VARCHAR(50) NOT NULL REFERENCES orders(order_id),
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    quantity  INTEGER     NOT NULL
)
--------------------------------------------------------------------------------
CREATE TABLE order_requests (
    id        SERIAL      PRIMARY KEY,
    order_id  VARCHAR(50) NOT NULL REFERENCES orders(order_id),
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    quantity  INTEGER     NOT NULL
)
--------------------------------------------------------------------------------
CREATE TABLE orders (
    order_id        VARCHAR(50)   PRIMARY KEY,
    order_date      DATE          NOT NULL DEFAULT CURRENT_DATE,
    customer_id     VARCHAR(50)   NOT NULL REFERENCES customers(customer_id),
    order_invoice   VARCHAR(100),
    subtotal        DECIMAL(10,2) NOT NULL,
    tax_amount      DECIMAL(10,2) NOT NULL DEFAULT 0,
    shipping_amount DECIMAL(10,2) NOT NULL DEFAULT 0,
    discount_applied DECIMAL(10,2) NOT NULL DEFAULT 0,   -- discount actually applied on the invoice
    charged_price   DECIMAL(10,2) NOT NULL,
    promo_code      VARCHAR(50)   REFERENCES promotions(promo_code),  -- promo the customer was entitled to
    refund_amount   DECIMAL(10,2),
    refund_status   VARCHAR(20),
    refund_reason   TEXT
)
--------------------------------------------------------------------------------
CREATE TABLE price_history (
    id             SERIAL        PRIMARY KEY,
    item_code      VARCHAR(50)   NOT NULL REFERENCES items(item_code),
    unit_price     DECIMAL(10,2) NOT NULL,
    effective_from DATE          NOT NULL,
    effective_to   DATE
)
--------------------------------------------------------------------------------
CREATE TABLE promotions (
    promo_code     VARCHAR(50)   PRIMARY KEY,
    description    VARCHAR(300),
    discount_type  VARCHAR(20)   NOT NULL,   -- 'percent' or 'flat'
    discount_value DECIMAL(10,2) NOT NULL
)
