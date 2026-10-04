@echo off
setlocal
if exist data\leaderboard.db del /q data\leaderboard.db
if exist data\leaderboard.db-wal del /q data\leaderboard.db-wal
if exist data\leaderboard.db-shm del /q data\leaderboard.db-shm
echo Local database removed. The next run will rebuild it from data\institutions.csv.
pause
