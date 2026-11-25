/* Reference formats for the sales reports, stored in FMTLIB.FORMATS. */
proc format library=fmtlib;
  value $region
    'N', 'NE', 'NW' = 'North'
    'S', 'SE', 'SW' = 'South'
    'E'             = 'East'
    'W'             = 'West'
    other           = 'Unassigned';

  value $channel
    'W' = 'Web'
    'S' = 'Store'
    'P' = 'Phone';

  value amtband
    low  -< 100   = 'Small'
    100  -< 1000  = 'Medium'
    1000 - high   = 'Large';

  invalue $regcode
    'North' = 'N'
    'South' = 'S';

  picture pctfmt (round)
    low - high = '009.9%' (multiplier=10);
run;
