
-- Load tracking data into the bdc2026_tracking table. Some periods are missing data, but I've elected to include all available data.
-- The file names are hardcoded. 
load data infile
'2025-11-28.Team.J.@.Team.K.Tracking_P1.csv'
into table moncton.bdc2026_tracking
FIELDS
    TERMINATED BY ','
    OPTIONALLY ENCLOSED BY '"'
  LINES
    TERMINATED BY '\r\n'
  IGNORE 1 LINES;


-- Rename column names, extract teams from the Image ID and change the Clock to time elapsed. 
drop table if exists tracking_tmp1;

Create temporary table tracking_tmp1 
select LEFT(`Image ID`,26) as game_id, period, `Image ID` as image_id, RIGHT(`Image ID`,6) as frame,
	CASE 
		when Period='1' then 1200-CAST(SUBSTRING_INDEX(`Game Clock`,':',1)*60+SUBSTRING_INDEX(`Game Clock`,':',-1) as signed)
        when Period='2' then 1200-CAST(SUBSTRING_INDEX(`Game Clock`,':',1)*60+SUBSTRING_INDEX(`Game Clock`,':',-1) as signed)+1200
        when Period='3' then 1200-CAST(SUBSTRING_INDEX(`Game Clock`,':',1)*60+SUBSTRING_INDEX(`Game Clock`,':',-1) as signed)+2400
        when Period='OT' then 300-CAST(SUBSTRING_INDEX(`Game Clock`,':',1)*60+SUBSTRING_INDEX(`Game Clock`,':',-1) as signed)+3600 
        end as time_elapsed,
	Team as venue, 
    LEFT(SUBSTRING_INDEX(`Image ID`,'@ ',-1),6) as home_team,
    RIGHT(SUBSTRING_INDEX(`Image ID`, ' @ ', 1),6) as away_team,
	`Player or Puck` as player_or_puck, `Player ID` as player_id, `Player Jersey Number` as player_jersey,
    `Rink Location X (feet)` as x_feet, `Rink Location Y (feet)` as y_feet, `Rink Location Z (feet)` as z_feet,
    `Goal Score` as goal_score, GoalieTeamOnRightSideOfRink1stPeriod
from bdc2026_tracking as t
left join bdc2026_camera as c
	on LEFT(`Image ID`,26)=c.game;


-- Define coordinates so that the Home team defensive zone is always on the right side. 
drop table if exists tracking_tmp2;

Create temporary table tracking_tmp2 select game_id, period, image_id, frame, time_elapsed, venue, 
	home_team, away_team, player_or_puck, player_id, player_jersey,  
	CASE
		when 'Home'=GoalieTeamOnRightSideOfRink1stPeriod and (period=2 or period='OT') and x_feet<>'' then -x_feet
        when 'Away'=GoalieTeamOnRightSideOfRink1stPeriod and (period=1 or period=3) and x_feet<>'' then -x_feet
        else x_feet END as x_feet,
	CASE
		when 'Home'=GoalieTeamOnRightSideOfRink1stPeriod and (period=2 or period='OT') and y_feet<>'' then -y_feet
        when 'Away'=GoalieTeamOnRightSideOfRink1stPeriod and (period=1 or period=3) and y_feet<>'' then -y_feet
        else y_feet END as y_feet,
	z_feet, goal_score
from tracking_tmp1;


-- Create a temporary table with the puck position. 
drop table if exists puck_position_tmp;

Create temporary table puck_position_tmp select image_id, x_feet, y_feet 
from tracking_tmp2
where player_or_puck='Puck';


-- Add the puck position by joining on image_id. 
drop table if exists tracking_tmp3;

Create temporary table tracking_tmp3 select game_id, period, t.image_id, frame, time_elapsed, venue, 
	CASE when venue='Home' then home_team when venue='away' then away_team else NULL END as team,
	home_team, away_team, player_id, player_jersey, t.x_feet, t.y_feet, z_feet, goal_score,
    p.x_feet as x_puck_feet, p.y_feet as y_puck_feet
from tracking_tmp2 as t
left join puck_position_tmp as p
	on t.image_id=p.image_id
where player_or_puck='Player';


-- Calculate the players distance to the puck using basic Pythagoras. 
drop table if exists tracking_tmp4;

Create temporary table tracking_tmp4 select *, 
	CASE when x_puck_feet<>'' 
	then SQRT(POW(x_feet-x_puck_feet, 2) + POW(y_feet-y_puck_feet, 2)) 
	else NULL END as distance_to_puck
from tracking_tmp3;

-- Calculate the rank in terms of distance to the puck - For both teams and for the individual teams. 
drop table if exists tracking_tmp5;

Create temporary table tracking_tmp5 select *, 
	CASE when distance_to_puck is NULL then NULL 
		else row_number() OVER(partition by image_id order by distance_to_puck) END as distance_rank,
	CASE when distance_to_puck is NULL then NULL 
		else row_number() OVER(partition by image_id, team order by distance_to_puck) END as distance_rank_team
from tracking_tmp4;


-- Find the player in possession. Rank=1 means being closest to the puck in the WINDOW function above.
-- Player has to be within 6 feet of the puck to be in possession of the puck. 
drop table if exists possession_player_tmp;

Create temporary table possession_player_tmp select image_id, player_id, team
from tracking_tmp5
where distance_rank=1 and distance_to_puck<6;


-- Add player_in_possession and team_in_possession columns - 1 for true and 0 for false. 
drop table if exists tracking_tmp6;

Create temporary table tracking_tmp6 select game_id, period, t.image_id, frame, time_elapsed, venue, 
	t.team, home_team, away_team, t.player_id, player_jersey,
    x_feet, y_feet, x_puck_feet, y_puck_feet, distance_to_puck, 
    distance_rank, distance_rank_team, goal_score,
	CASE when t.player_id=p.player_id then 1 else 0 END as player_in_possession,
    CASE when t.team=p.team then 1 else 0 END as team_in_possession
from tracking_tmp5 as t
left join possession_player_tmp as p
	on t.image_id=p.image_id;



-- Load event data into the bdc2026_events table. Hardcoded file name. 
load data infile
'2025-11-28.Team.J.@.Team.K.Events.csv'
into table moncton.bdc2026_events
FIELDS
    TERMINATED BY ','
    OPTIONALLY ENCLOSED BY '"'
  LINES
    TERMINATED BY '\r\n'
  IGNORE 1 LINES;


-- Clean column names, change Clock to time_elapsed, find strength_state and score_state and add is_corsi, is_fenwick,...
drop table if exists events_tmp1;

Create temporary table events_tmp1 select date, home_team, away_team, period, 
	CASE 
		when Period='1' then 1200-CAST(SUBSTRING_INDEX(Clock,':',1)*60+SUBSTRING_INDEX(Clock,':',-1) as signed)
        when Period='2' then 1200-CAST(SUBSTRING_INDEX(Clock,':',1)*60+SUBSTRING_INDEX(Clock,':',-1) as signed)+1200
        when Period='3' then 1200-CAST(SUBSTRING_INDEX(Clock,':',1)*60+SUBSTRING_INDEX(Clock,':',-1) as signed)+2400
        when Period='4' then 300-CAST(SUBSTRING_INDEX(Clock,':',1)*60+SUBSTRING_INDEX(Clock,':',-1) as signed)+3600 
        end as time_elapsed,
	CASE when Team=Home_Team then 'Home' when Team=Away_Team then 'Away' else '' END as venue,
    team,
    CASE 
		when Team=Home_Team then CONCAT(Home_Team_Skaters,'v',Away_Team_Skaters)
        when Team=Away_Team then CONCAT(Away_Team_Skaters,'v',Home_Team_Skaters)
        else '' END as strength_state,
	CONCAT(Home_Team_Skaters,'v',Away_Team_Skaters) as home_strength_state,
	CASE 
		when Team=Home_Team then Home_Team_Goals - Away_Team_Goals 
        when Team=Away_Team then Away_Team_Goals - Home_Team_Goals
        else '' END as score_state,
	Home_Team_Goals - Away_Team_Goals as home_score_state,
	player_id, event, X_Coordinate as x1, Y_Coordinate as y1, 
    detail_1, detail_2, detail_3, detail_4, player_id_2, X_Coordinate_2 as x2, Y_Coordinate_2 as y2,
    CASE when event='Shot' or event='Goal' then 1 else 0 END as is_corsi,
    CASE when (event='Shot' and detail_2<>'Blocked') or event='Goal' then 1 else 0 END as is_fenwick,
    CASE when (event='Shot' and detail_2='On Net') or event='Goal' then 1 else 0 END as is_shot,
    CASE when event='Goal' then 1 else 0 END as is_goal,
    row_number() over() as event_index
from bdc2026_events;


-- Find the time_elapsed for the start of all strength_state changes. 
drop table if exists strength_tmp1;

Create temporary table strength_tmp1 select 
	CONCAT(date,' ',away_team,' @ ',home_team) as game_id,
	home_strength_state, time_elapsed, event_index
from events_tmp1;


drop table if exists strength_tmp2;

Create temporary table strength_tmp2 select game_id, home_strength_state, time_elapsed, event_index,
	CASE 
		when home_strength_state <=> LAG(home_strength_state) OVER(partition by game_id order by event_index) then 0 
        else 1 END as changed
from strength_tmp1;


drop table if exists strength_tmp3;

Create temporary table strength_tmp3 select game_id, home_strength_state, time_elapsed, event_index,
	SUM(changed) OVER(partition by game_id order by event_index rows unbounded preceding) as run_id
from strength_tmp2;


drop table if exists strength_tmp4;

Create temporary table strength_tmp4 select
	game_id, home_strength_state, run_id,
	MIN(time_elapsed) as time_elapsed,
	MIN(event_index) as first_row
from strength_tmp3
group by game_id, home_strength_state, run_id
order by game_id, time_elapsed;


-- Find the start_time and end_time of all strength_state changes. 
-- The last strength_state of the game is set to end at 5000 seconds. It's just important that the game ends before the strength_state window ends. 
drop table if exists strength_tmp5;

Create temporary table strength_tmp5 
select game_id, home_strength_state,
	CASE when time_elapsed=0 then 0 else time_elapsed+1 END as start_time,
    COALESCE(LEAD(time_elapsed) OVER(partition by game_id order by time_elapsed),5000) as end_time
from strength_tmp4;


-- Find all score_state changes. 
drop table if exists goals_tmp1;

Create table goals_tmp1 select 
	CONCAT(date,' ',away_team, ' @ ', home_team) as game_id, time_elapsed, 
    CASE when venue='Home' then home_score_state+1 when venue='Away' then home_score_state-1 else 0 END as home_score_state
from events_tmp1
where event='Goal'
order by date, home_team, time_elapsed;



-- Add the start of each game, where the score_state is 0. 
drop table if exists goals_tmp2;

Create temporary table goals_tmp2 
select * from goals_tmp1
UNION ALL
select distinct game_id, 0 as time_elapsed, 0 as home_score_state
from goals_tmp1;

drop table if exists goals_tmp1;


-- Find the start_time and end_time of all score_state changes. 
-- The last score_state of the game is set to end at 5000 seconds. It's just important that the game ends before the score_state window ends. 
drop table if exists goals_tmp3;

Create temporary table goals_tmp3 select game_id, home_score_state,
	CASE when time_elapsed=0 then 0 else time_elapsed+1 END as start_time,
    COALESCE(LEAD(time_elapsed) OVER(partition by game_id order by time_elapsed),5000) as end_time
from goals_tmp2
order by game_id, time_elapsed;


-- Add strength_state and score_state to the final cleaned tracking data table. 
-- Defense_role is defined as the defensive player closest to the puck or any defensive players within 6 feet of the puck. All other defenders are categorized as Secondary defenders. 
Create table bdc2026_tracking_cleaned select t.game_id, period, image_id, frame, time_elapsed, venue, 
	team, player_id, player_jersey, x_feet, y_feet, x_puck_feet, y_puck_feet, 
    distance_to_puck, distance_rank, distance_rank_team, player_in_possession, team_in_possession,
    CASE 
		when team_in_possession=0 and (distance_rank_team=1 or distance_to_puck<6) then 'Primary'
        when team_in_possession=0 then 'Secondary'
        else '' END as defense_role,
    CASE 
        when venue='Away' and home_strength_state='4v5' then '5v4'
        when venue='Away' and home_strength_state='5v4' then '4v5'
        when venue='Away' and home_strength_state='6v5' then '5v6'
        when venue='Away' and home_strength_state='6v4' then '4v6'
        when venue='Away' and home_strength_state='4v6' then '6v4'
        when venue='Away' and home_strength_state='3v5' then '5v3'
        when venue='Away' and home_strength_state='5v6' then '6v5'
        when venue='Away' and home_strength_state='3v4' then '4v3'
        when venue='Away' and home_strength_state='4v3' then '3v4'
        when venue='Away' and home_strength_state='5v3' then '3v5'
        else home_strength_state END as strength_state,
	CASE 
		when venue='Away' and home_score_state<>0 then -home_score_state
		else home_score_state END as score_state
from tracking_tmp6 as t
left join strength_tmp5 as s
	on t.game_id=s.game_id and t.time_elapsed>=s.start_time and t.time_elapsed<=s.end_time
left join goals_tmp3 as g
	on t.game_id=g.game_id and t.time_elapsed>=g.start_time and t.time_elapsed<=g.end_time;


-- One row per player per frame, and that pair is unique in the data. Clustering
-- on image_id means reading a frame is one sequential read, which is what almost
-- every tracking query starts from.
alter table bdc2026_tracking_cleaned
    add primary key (image_id, player_id);

-- The two things you will filter on next: a stretch of a game, and one player.
-- One statement so the 10 million rows are scanned once rather than three times.
alter table bdc2026_tracking_cleaned
    add index ix_game_time (game_id, time_elapsed),
    add index ix_player_game_time (player_id, game_id, time_elapsed);

-- Optional, only if you query possession spells directly. Low cardinality on its
-- own, but the leading game_id and trailing time_elapsed carry it.
alter table bdc2026_tracking_cleaned
    add index ix_game_team_possession (game_id, team_in_possession, time_elapsed);

analyze table bdc2026_tracking_cleaned;


-- Naming the 9 faceoff dots. In the eventdata the x1,y1 coordinates only has a few possibilities for Faceoff Win events.  
drop table if exists faceoff_tmp1;

Create temporary table faceoff_tmp1 select 
	CONCAT(date,' ',away_team, ' @ ', home_team) as game_id, time_elapsed, 
	CASE 
	when x1=0 and (y1=0.5 or y1=-0.5) then 'N01'
    when (venue='Home' and x1=20 and y1=22.5) or (venue='Away' and x1=-20 and y1=-21.5) then 'N02'
    when (venue='Home' and x1=20 and y1=-21.5) or (venue='Away' and x1=-20 and y1=22.5) then 'N03'
    when (venue='Home' and x1=-20 and y1=22.5) or (venue='Away' and x1=20 and y1=-21.5) then 'N04'
    when (venue='Home' and x1=-20 and y1=-21.5) or (venue='Away' and x1=20 and y1=22.5) then 'N05'
    when (venue='Home' and x1=69 and (y1=22.5 or y1=21.5)) or (venue='Away' and x1=-69 and y1=22.5) then 'O01'
    when (venue='Home' and x1=69 and (y1=-22.5 or y1=-21.5)) or (venue='Away' and x1=-69 and y1=-21.5) then 'O02'
    when (venue='Home' and x1=-69 and y1=22.5) or (venue='Away' and x1=69 and (y1=-21.5 or y1=-22.5)) then 'D01'
    when (venue='Home' and x1=-69 and y1=-21.5) or (venue='Away' and x1=69 and (y1=22.5 or y1=21.5)) then 'D02'
    else '' END faceoff_position,
    CASE when venue='Away' and x1<>'' then -x1 else x1 END as faceoff_x,
    CASE 
	when x1=0 and (y1=0.5 or y1=-0.5) then 0
    when (venue='Home' and x1=20 and y1=22.5) or (venue='Away' and x1=-20 and y1=-21.5) then 22
    when (venue='Home' and x1=20 and y1=-21.5) or (venue='Away' and x1=-20 and y1=22.5) then -22
    when (venue='Home' and x1=-20 and y1=22.5) or (venue='Away' and x1=20 and y1=-21.5) then 22
    when (venue='Home' and x1=-20 and y1=-21.5) or (venue='Away' and x1=20 and y1=22.5) then -22
    when (venue='Home' and x1=69 and (y1=22.5 or y1=21.5)) or (venue='Away' and x1=-69 and y1=22.5) then 22
    when (venue='Home' and x1=69 and (y1=-22.5 or y1=-21.5)) or (venue='Away' and x1=-69 and y1=-21.5) then -22
    when (venue='Home' and x1=-69 and y1=22.5) or (venue='Away' and x1=69 and (y1=-21.5 or y1=-22.5)) then 22
    when (venue='Home' and x1=-69 and y1=-21.5) or (venue='Away' and x1=69 and (y1=22.5 or y1=21.5)) then -22
    else '' END faceoff_y
from events_tmp1
where event='Faceoff Win' and strength_state='5v5'
order by date, home_team, time_elapsed;


-- Can run this to see all 'Faceoff Win' coordinates. 
select x1, y1
from events_tmp1 
where event='Faceoff Win'
group by x1, y1;



-- There are frame gaps with every stoppage, so in order to find faceoff positions we need to find the frame after the gap. 
-- group by frame to get the time_elapsed for each frame. 
drop table if exists frame_gaps_tmp1;

Create temporary table frame_gaps_tmp1 select game_id, image_id, frame, time_elapsed
from tracking_tmp5
group by game_id, image_id, frame, time_elapsed;


-- Find the gaps between frames and set the start of the game to a 5000 frame gap. 
drop table if exists frame_gaps_tmp2;

Create temporary table frame_gaps_tmp2 select *,
	COALESCE(frame - LAG(frame) OVER(partition by game_id order by image_id),5000) as frame_gap
from frame_gaps_tmp1;


-- Only include frames where the gap from the previous frame is at least 100. 
drop table if exists frame_gaps_tmp3;

Create temporary table frame_gaps_tmp3 select * 
from frame_gaps_tmp2 where frame_gap>100;


-- Find all neutral zone faceoffs, which will be used to determine player positions. 
drop table if exists faceoff_tmp2;

Create temporary table faceoff_tmp2 select f.game_id, frame, faceoff_position, faceoff_x, faceoff_y
from frame_gaps_tmp3 as f
left join faceoff_tmp1 as s
	on f.game_id=s.game_id and f.time_elapsed=s.time_elapsed
where faceoff_position is not NULL and LEFT(faceoff_position,1)='N'
order by game_id, frame;


-- Determine player positions based on the positioning on neutral zone faceoffs. 
drop table if exists positions_tmp1;

Create temporary table positions_tmp1 select f.game_id, team, player_id, 
	CASE 
		when venue='Home' and x_feet-faceoff_x<7 then 'F'
        when venue='Away' and faceoff_x-x_feet<7 then 'F'
        when venue='Home' and x_feet-faceoff_x>7 then 'D'
        when venue='Away' and faceoff_x-x_feet>7 then 'D'
        else '' END as position,
	CASE 
		when venue='Home' and y_feet-faceoff_y>=3 then 'R'
        when venue='Home' and faceoff_y-y_feet>=3 then 'L'
        when venue='Home' and y_feet-faceoff_y<3 and faceoff_y-y_feet<3 then 'C'
        when venue='Away' and faceoff_y-y_feet>3 then 'R'
        when venue='Away' and y_feet-faceoff_y>3 then 'L'
        when venue='Away' and y_feet-faceoff_y<3 and y_feet-faceoff_y<3 then 'C'
        else '' END as side
from faceoff_tmp2 as f
left join tracking_tmp6 as t
	on f.game_id=t.game_id and f.frame=t.frame and t.player_id<>'Go'
order by f.game_id, f.frame;

select team, player_id,
	SUM(CASE when position='F' then 1 else 0 END) as F,
    SUM(CASE when position='D' then 1 else 0 END) as D,
    SUM(CASE when side='R' then 1 else 0 END) as R,
    SUM(CASE when side='L' then 1 else 0 END) as L,
    SUM(CASE when side='C' then 1 else 0 END) as C
from positions_tmp1
group by team, player_id
order by F desc, D desc;