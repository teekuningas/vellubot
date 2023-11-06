# A simple irc bot to alert on new rss feed items.

## Install deps

Use nix to get necessary dependencies in a development shell:  
  
```
$ make shell  
```
  

## In the development shell, run the fetchers and parsers:
  
```
$ make watch_test  
```
  
## In the development shell, run the irc bot (including fetchers and parsers):
  
```
$ make watch  
```
  
