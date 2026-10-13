CREATE TABLE IF NOT EXISTS product_details (
    item_code TEXT PRIMARY KEY REFERENCES items(item_code),
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    features_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(features_json) AND json_type(features_json)='object')
);
CREATE INDEX IF NOT EXISTS ix_product_category ON product_details(category);
