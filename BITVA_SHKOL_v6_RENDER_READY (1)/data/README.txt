Seed data for БИТВА ШКОЛ.

The packaged institutions.csv is cleaned at build time from the provided parser output:
- only Russia is public by default (PUBLIC_COUNTRIES=RU)
- parser UI/filter rows are removed
- exact duplicates are removed
- conservative school-number aliases such as "МАОУ СОШ №21" and "Средняя школа №21" are merged

To add more countries later, use the admin CSV import. Public visibility is controlled by PUBLIC_COUNTRIES.
