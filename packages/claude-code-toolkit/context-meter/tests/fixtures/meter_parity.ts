// The cases the guard (pytest) and the port (claude plugin test) both read: a JSON document after `export default`.
// A test module can import code files only, hence the .ts name.
export default {
 "settings_cases": [
  {
   "name": "no settings files: the defaults",
   "project": null,
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "project file with percentages",
   "project": "{\"warn_pct\": 50, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     100000.0,
     120000.0,
     140000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "project file wins over the user file",
   "project": "{\"warn_pct\": 40, \"force_pct\": 50, \"hard_pct\": 60}",
   "home": "{\"warn_pct\": 10, \"force_pct\": 20, \"hard_pct\": 30}",
   "expect": {
    "thresholds": [
     80000.0,
     100000.0,
     120000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "user file used when the project has none",
   "project": null,
   "home": "{\"window_tokens\": 1000000}",
   "expect": {
    "thresholds": [
     550000.0,
     650000.0,
     750000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 1000000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "invalid JSON in the project file stops the search: defaults",
   "project": "{not json",
   "home": "{\"warn_pct\": 10, \"force_pct\": 20, \"hard_pct\": 30}",
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "a JSON array in the project file stops the search: defaults",
   "project": "[1, 2]",
   "home": "{\"warn_pct\": 10, \"force_pct\": 20, \"hard_pct\": 30}",
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "an empty project file stops the search: defaults",
   "project": "",
   "home": "{\"warn_pct\": 10, \"force_pct\": 20, \"hard_pct\": 30}",
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "window_tokens that is not an integer falls back",
   "project": "{\"window_tokens\": \"500000\", \"warn_pct\": 50, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     100000.0,
     120000.0,
     140000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "window_tokens written as a float falls back",
   "project": "{\"window_tokens\": 400000.0}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "window_tokens that is a boolean falls back",
   "project": "{\"window_tokens\": true}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "window_tokens of zero or less falls back",
   "project": "{\"window_tokens\": 0}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "a larger window scales the percentages",
   "project": "{\"window_tokens\": 1000000}",
   "home": null,
   "expect": {
    "thresholds": [
     550000.0,
     650000.0,
     750000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 1000000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "warn_pct alone completes with the other defaults",
   "project": "{\"warn_pct\": 50}",
   "home": null,
   "expect": {
    "thresholds": [
     100000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "warn_pct alone that breaks the order drops all three",
   "project": "{\"warn_pct\": 80}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "hard_pct of 100 is allowed",
   "project": "{\"hard_pct\": 100}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     200000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "hard_pct above 100 drops all three",
   "project": "{\"warn_pct\": 10, \"force_pct\": 20, \"hard_pct\": 101}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "a percentage that is a string drops all three",
   "project": "{\"warn_pct\": \"50\", \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "a percentage that is a boolean drops all three",
   "project": "{\"warn_pct\": true, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "fractional percentages are numbers",
   "project": "{\"warn_pct\": 50.5, \"force_pct\": 60.5, \"hard_pct\": 70.5}",
   "home": null,
   "expect": {
    "thresholds": [
     101000.0,
     121000.0,
     141000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "warn_pct equal to force_pct drops all three",
   "project": "{\"warn_pct\": 60, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "a null percentage drops all three",
   "project": "{\"warn_pct\": null, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "absolute tokens",
   "project": "{\"warn_tokens\": 2000, \"force_tokens\": 4000, \"hard_tokens\": 6000}",
   "home": null,
   "expect": {
    "thresholds": [
     2000,
     4000,
     6000
    ],
    "from_pct": false,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "absolute tokens win over percentages",
   "project": "{\"warn_tokens\": 2000, \"force_tokens\": 4000, \"hard_tokens\": 6000, \"warn_pct\": 10, \"force_pct\": 20, \"hard_pct\": 30}",
   "home": null,
   "expect": {
    "thresholds": [
     2000,
     4000,
     6000
    ],
    "from_pct": false,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "only warn_tokens: percentages decide",
   "project": "{\"warn_tokens\": 2000}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "two of three token keys: percentages decide",
   "project": "{\"warn_tokens\": 2000, \"force_tokens\": 4000, \"warn_pct\": 50, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     100000.0,
     120000.0,
     140000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "tokens out of order: percentages decide",
   "project": "{\"warn_tokens\": 4000, \"force_tokens\": 2000, \"hard_tokens\": 6000}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "tokens that are floats: percentages decide",
   "project": "{\"warn_tokens\": 2000.0, \"force_tokens\": 4000, \"hard_tokens\": 6000}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "a token of zero: percentages decide",
   "project": "{\"warn_tokens\": 0, \"force_tokens\": 4000, \"hard_tokens\": 6000}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "tokens with an exponent are floats",
   "project": "{\"warn_tokens\": 2e3, \"force_tokens\": 4000, \"hard_tokens\": 6000}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "tokens win while the percentage triple is invalid",
   "project": "{\"warn_tokens\": 2000, \"force_tokens\": 4000, \"hard_tokens\": 6000, \"warn_pct\": 90, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     2000,
     4000,
     6000
    ],
    "from_pct": false,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "tokens in the user file when the project has none",
   "project": null,
   "home": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000}",
   "expect": {
    "thresholds": [
     1000,
     2000,
     3000
    ],
    "from_pct": false,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "enabled false",
   "project": "{\"enabled\": false}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": false,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "enabled that is not a boolean falls back to true",
   "project": "{\"enabled\": \"no\"}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "show_meter false",
   "project": "{\"show_meter\": false}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": false,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "show_meter that is not a boolean falls back to true",
   "project": "{\"show_meter\": 0}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "min_work_tokens set",
   "project": "{\"min_work_tokens\": 5000}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 5000
   }
  },
  {
   "name": "min_work_tokens of zero falls back",
   "project": "{\"min_work_tokens\": 0}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "min_work_tokens that is negative falls back",
   "project": "{\"min_work_tokens\": -5}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "min_work_tokens that is a float falls back",
   "project": "{\"min_work_tokens\": 5000.5}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "unknown keys change nothing",
   "project": "{\"something_else\": 1, \"warn_pct\": 50, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     100000.0,
     120000.0,
     140000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "a bad key does not spoil a good one",
   "project": "{\"enabled\": \"x\", \"show_meter\": false, \"window_tokens\": 300000}",
   "home": null,
   "expect": {
    "thresholds": [
     165000.0,
     195000.0,
     225000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": false,
    "window_tokens": 300000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "NaN as a percentage drops all three",
   "project": "{\"warn_pct\": NaN, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     110000.0,
     130000.0,
     150000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  },
  {
   "name": "a duplicate key: the last one wins",
   "project": "{\"warn_pct\": 10, \"warn_pct\": 50, \"force_pct\": 60, \"hard_pct\": 70}",
   "home": null,
   "expect": {
    "thresholds": [
     100000.0,
     120000.0,
     140000.0
    ],
    "from_pct": true,
    "enabled": true,
    "show_meter": true,
    "window_tokens": 200000,
    "min_work_tokens": 20000
   }
  }
 ],
 "guard_cases": [
  {
   "name": "fresh session below warn",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000}",
   "size": 500,
   "start_size": null,
   "expect_level": "low"
  },
  {
   "name": "fresh session in warn",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000}",
   "size": 1500,
   "start_size": null,
   "expect_level": "low"
  },
  {
   "name": "fresh session at force",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000}",
   "size": 2000,
   "start_size": null,
   "expect_level": "force"
  },
  {
   "name": "fresh session at hard",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000}",
   "size": 3000,
   "start_size": null,
   "expect_level": "hard"
  },
  {
   "name": "injected session at force, grown too little",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000, \"min_work_tokens\": 500}",
   "size": 2100,
   "start_size": 1800,
   "expect_level": "low"
  },
  {
   "name": "injected session at force, grown exactly min_work",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000, \"min_work_tokens\": 500}",
   "size": 2300,
   "start_size": 1800,
   "expect_level": "force"
  },
  {
   "name": "injected session at hard, grown too little",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000, \"min_work_tokens\": 500}",
   "size": 3100,
   "start_size": 2800,
   "expect_level": "low"
  },
  {
   "name": "injected session at hard, grown enough",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000, \"min_work_tokens\": 500}",
   "size": 3400,
   "start_size": 2800,
   "expect_level": "hard"
  },
  {
   "name": "injected session in warn is never held",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000, \"min_work_tokens\": 500}",
   "size": 1500,
   "start_size": 1400,
   "expect_level": "low"
  },
  {
   "name": "injected session below warn",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000, \"min_work_tokens\": 500}",
   "size": 100,
   "start_size": 50,
   "expect_level": "low"
  },
  {
   "name": "start size zero still holds",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000, \"min_work_tokens\": 2500}",
   "size": 2100,
   "start_size": 0,
   "expect_level": "low"
  },
  {
   "name": "size below the start size holds",
   "project": "{\"warn_tokens\": 1000, \"force_tokens\": 2000, \"hard_tokens\": 3000, \"min_work_tokens\": 500}",
   "size": 2100,
   "start_size": 2500,
   "expect_level": "low"
  },
  {
   "name": "default min_work_tokens with default percentages",
   "project": null,
   "size": 140000,
   "start_size": 130000,
   "expect_level": "low"
  },
  {
   "name": "default min_work_tokens, enough growth",
   "project": null,
   "size": 160000,
   "start_size": 130000,
   "expect_level": "hard"
  }
 ]
}
