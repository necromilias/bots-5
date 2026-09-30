| # | value (repr, truncated) | baseline SQL | candidate SQL | Python | agree |
|---|---|---|---|---|---|
| 1 | `None` | None | None | False | yes |
| 2 | `''` | 0 | 0 | False | yes |
| 3 | `'token'` | 1 | 1 | True | yes |
| 4 | `'TOKEN'` | 1 | 1 | True | yes |
| 5 | `'Token'` | 1 | 1 | True | yes |
| 6 | `'nekto'` | 1 | 1 | False | yes |
| 7 | `'enotk'` | 1 | 1 | False | yes |
| 8 | `'otken'` | 1 | 1 | False | yes |
| 9 | `'to!ken'` | 1 | 1 | True | yes |
| 10 | `'t_o-k.e n'` | 1 | 1 | True | yes |
| 11 | `'to ken'` | 1 | 1 | True | yes |
| 12 | `'apricotKey'` | 0 | 0 | False | yes |
| 13 | `'token1'` | 0 | 0 | False | yes |
| 14 | `'1token'` | 0 | 0 | False | yes |
| 15 | `'tokenx'` | 0 | 0 | False | yes |
| 16 | `'xtoken'` | 0 | 0 | False | yes |
| 17 | `'tokén'` | 0 | 0 | False | yes |
| 18 | `'tokén'` | 0 | 0 | False | yes |
| 19 | `'password'` | 1 | 1 | True | yes |
| 20 | `'paßword'` | 1 | 1 | True | yes |
| 21 | `'paẞword'` | 1 | 1 | True | yes |
| 22 | `'paſsword'` | 1 | 1 | True | yes |
| 23 | `'paẘssord'` | 1 | 1 | False | yes |
| 24 | `'passẘord'` | 1 | 1 | True | yes |
| 25 | `'toKen'` | 1 | 1 | True | yes |
| 26 | `'apiİey'` | 0 | 0 | False | yes |
| 27 | `'tokeŉ'` | 1 | 1 | True | yes |
| 28 | `'accesﬅoken'` | 1 | 1 | True | yes |
| 29 | `'refreſhtoken'` | 1 | 1 | True | yes |
| 30 | `'secreṫ'` | 1 | 1 | True | yes |
| 31 | `'passwordé'` | 1 | 1 | True | yes |
| 32 | `'péssword'` | 0 | 0 | False | yes |
| 33 | `'password\x00x'` | 1 | 1 | False | yes |
| 34 | `'\x00token'` | 1 | 1 | True | yes |
| 35 | `'to\x00ken'` | 1 | 1 | True | yes |
| 36 | `'password\x00'` | 1 | 1 | True | yes |
| 37 | `'\x00'` | 1 | 1 | False | yes |
| 38 | `'token\n'` | 1 | 1 | True | yes |
| 39 | `'token\t'` | 1 | 1 | True | yes |
| 40 | `'token '` | 1 | 1 | True | yes |
| 41 | `'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa...` | 0 | 0 | False | yes |
| 42 | `'token!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!...` | 1 | 1 | True | yes |
| 43 | `'!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!...` | 1 | 1 | True | yes |
| 44 | `'🙂token'` | 1 | 1 | True | yes |
| 45 | `'token🙂'` | 1 | 1 | True | yes |
