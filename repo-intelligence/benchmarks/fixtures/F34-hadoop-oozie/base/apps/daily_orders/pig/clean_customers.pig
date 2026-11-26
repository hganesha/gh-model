-- Clean the customer extract and publish it to the warehouse.
%default INPUT '/data/raw/customers'
REGISTER 'udfs/piggybank.jar';

raw = LOAD '$INPUT' USING PigStorage(',') AS (customer_id:long, name:chararray, region:chararray, email:chararray, signup:chararray);
valid = FILTER raw BY customer_id IS NOT NULL AND region != '';
clean = FOREACH valid GENERATE customer_id, TRIM(name) AS name, UPPER(region) AS region,
        (email IS NULL ? 'unknown' : email) AS email, ToDate(signup, 'yyyy-MM-dd') AS signup_date;
uniq = DISTINCT clean;
STORE uniq INTO 'dim.customers' USING org.apache.hive.hcatalog.pig.HCatStorer();

/* The ten regions with the most customers. */
by_region = GROUP uniq BY region;
counts = FOREACH by_region GENERATE group AS region, COUNT(uniq) AS customers;
ordered = ORDER counts BY customers DESC;
top = LIMIT ordered 10;
STORE top INTO '/data/mart/top_regions' USING PigStorage(',');
