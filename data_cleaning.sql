-- ============================================================================
-- Big Data Cup 2026: cleaning the tracking and event files into two tables
-- ============================================================================
-- Reads the release's per-period tracking CSVs and one event CSV per game and
-- produces:
--
--   bdc2026_tracking_cleaned   one row per player per frame, with the puck's
--                              position, who has it, the defensive role, the
--                              strength and score state, and a rink zone
--   bdc2026_events_cleaned     one row per event, with the on-ice players
--                              pivoted into for_1..for_6 / against_1..against_6
--   bdc2026_players            the jersey to player lookup both tables resolve
--                              through, and the defensive metrics
--
-- The script runs top to bottom and is meant to be re-run from scratch: every
-- temporary table is dropped before it is built, so a failed run leaves nothing
-- half-written behind.
--
-- The numbered banners below are the order of work. Read in sequence they are
-- the shortest description of what the cleaning does.
-- ============================================================================



-- ============================================================================
-- 1. Load the tracking files
-- ----------------------------------------------------------------------------
-- One CSV per period, everything as text. Some periods are missing from the
-- release and are simply not loaded; the gaps show up later as missing frames.
-- ============================================================================
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




-- ============================================================================
-- 2. Name the columns and put the game on one timeline
-- ----------------------------------------------------------------------------
-- Image Id packs the game and the frame into one string, and the countdown clock
-- restarts every period. Both are unpacked here so periods can be compared and so
-- the events can be joined on time.
-- ============================================================================
-- Rename column names, extract teams from the Image ID and change the Clock to time elapsed. 
drop table if exists tracking_tmp1;

Create temporary table tracking_tmp1 
select LEFT(`Image ID`,26) as game_id, period, `Image ID` as image_id, CAST(RIGHT(`Image ID`,6) as unsigned) as frame,
	CASE 
		when Period='1' then 1200-CAST(SUBSTRING_INDEX(`Game Clock`,':',1)*60+SUBSTRING_INDEX(`Game Clock`,':',-1) as signed)
        when Period='2' then 1200-CAST(SUBSTRING_INDEX(`Game Clock`,':',1)*60+SUBSTRING_INDEX(`Game Clock`,':',-1) as signed)+1200
        when Period='3' then 1200-CAST(SUBSTRING_INDEX(`Game Clock`,':',1)*60+SUBSTRING_INDEX(`Game Clock`,':',-1) as signed)+2400
        when Period='OT' then 300-CAST(SUBSTRING_INDEX(`Game Clock`,':',1)*60+SUBSTRING_INDEX(`Game Clock`,':',-1) as signed)+3600 
        end as time_elapsed,
	Team as venue, 
    LEFT(SUBSTRING_INDEX(`Image ID`,'@ ',-1),6) as home_team,
    RIGHT(SUBSTRING_INDEX(`Image ID`, ' @ ', 1),6) as away_team,
	`Player or Puck` as player_or_puck, `Player ID` as player_id, 
    `Player Jersey Number` as player_jersey,
    CAST(NULLIF(`Rink Location X (feet)`, '') as double) as x_feet, 
    CAST(NULLIF(`Rink Location Y (feet)`, '') as double) as y_feet, 
    CAST(NULLIF(`Rink Location Z (feet)`, '') as double) as z_feet,
    `Goal Score` as goal_score, GoalieTeamOnRightSideOfRink1stPeriod
from bdc2026_tracking as t
left join bdc2026_camera as c
	on LEFT(`Image ID`,26)=c.game;




-- ============================================================================
-- 3. Face the rink one way
-- ----------------------------------------------------------------------------
-- Teams change ends between periods, so without this a position from period 1 and
-- the same position from period 2 are mirrored. The camera table says which team
-- had its goalie on the right in the first period; the periods that leave the home
-- end the wrong way are rotated 180 degrees - both x and y, not x alone, so a
-- player keeps their own side of the ice.
-- ============================================================================
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




-- ============================================================================
-- 4. Put the puck on every player's row
-- ----------------------------------------------------------------------------
-- The puck is a row like any other. Joining its position onto every player in the
-- same frame is what makes distance to the puck, and therefore possession,
-- possible at all.
-- ============================================================================
-- Create a temporary table with the puck position. 
drop table if exists puck_position_tmp;

Create temporary table puck_position_tmp select image_id, x_feet, y_feet 
from tracking_tmp2
where player_or_puck='Puck';


-- Add the puck position by joining on image_id. 
drop table if exists tracking_tmp3;

Create temporary table tracking_tmp3 select game_id, period, t.image_id, frame, time_elapsed, venue, 
	CASE when venue='Home' then home_team when venue='away' then away_team else NULL END as team,
	home_team, away_team, CAST(player_id as unsigned) as player_id, player_jersey, 
    t.x_feet, t.y_feet, z_feet, goal_score,
    p.x_feet as x_puck_feet, p.y_feet as y_puck_feet
from tracking_tmp2 as t
left join puck_position_tmp as p
	on t.image_id=p.image_id
where player_or_puck='Player';


-- Calculate the players distance to the puck using basic Pythagoras. 
drop table if exists tracking_tmp4;

Create temporary table tracking_tmp4 select *, 
	CASE when x_puck_feet is not NULL
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





-- ============================================================================
-- 5. Load the events
-- ----------------------------------------------------------------------------
-- One CSV per game, again as text so nothing is silently cast.
-- ============================================================================
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




-- ============================================================================
-- 6. Give each event its strength state and score state
-- ----------------------------------------------------------------------------
-- The event file carries who was on the ice, so the states are derived here rather
-- than looked up. Corsi, Fenwick, shots and goals are flagged in the same pass.
-- ============================================================================
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




-- ============================================================================
-- 7. Turn the strength state into windows
-- ----------------------------------------------------------------------------
-- A game returns to states it has already been in - 5v5, 4v4, 5v5 again - so these
-- are numbered spells, not groups. Three steps: flag where the state changes,
-- number the runs, then take the first row of each run.
-- ============================================================================
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




-- ============================================================================
-- 8. Turn the score into windows
-- ----------------------------------------------------------------------------
-- The same gaps and islands shape, driven by the goals rather than the penalties,
-- with the score starting at 0-0 before the first goal.
-- ============================================================================
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




-- ============================================================================
-- 9. Name the faceoff dots
-- ----------------------------------------------------------------------------
-- Faceoff coordinates only take a handful of distinct values in the event data, so
-- they can be named once and matched exactly. The names are used to find the
-- neutral zone ones, which is what the positions are estimated from.
-- ============================================================================
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





-- ============================================================================
-- 10. Find the frame a faceoff was taken on
-- ----------------------------------------------------------------------------
-- The tracker stops drawing frames while the play is stopped, so the first frame
-- after a gap is the lineup the faceoff was taken from. A gap of more than 100
-- frames counts as a stoppage; the first row of a game is given a 5000 frame gap
-- so it counts as one too.
-- ============================================================================
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




-- ============================================================================
-- 11. Estimate each player's position and side
-- ----------------------------------------------------------------------------
-- There is no position column in the release. It is inferred from where players
-- stand at neutral zone faceoffs. With ten games this is an estimate, not a record
-- - see the note on the data page.
-- ============================================================================
-- Determine player positions based on the positioning on neutral zone faceoffs. 
drop table if exists positions_tmp1;

Create temporary table positions_tmp1 select f.game_id, team, player_id, player_jersey,
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
	on f.game_id=t.game_id and f.frame=t.frame and t.player_jersey<>'Go' and t.player_jersey<>''
order by f.game_id, f.frame;



drop table if exists positions_tmp2;

Create temporary table positions_tmp2 select team, player_jersey,
	COUNT(*) as count,
	SUM(CASE when position='F' then 1 else 0 END) as F,
    SUM(CASE when position='D' then 1 else 0 END) as D,
    SUM(CASE when side='R' then 1 else 0 END) as R,
    SUM(CASE when side='L' then 1 else 0 END) as L,
    SUM(CASE when side='C' then 1 else 0 END) as C
from positions_tmp1
group by team, player_jersey
order by F desc, D desc;


drop table if exists positions_tmp3;

Create temporary table positions_tmp3 select team, player_jersey, count,
	CASE 
		when F/count>0.5 then 'F'
        when F/count<0.5 then 'D'
        else 'Undetermined' END as position,
	CASE 
		when R/count>0.5 then 'R'
        when L/count>0.5 then 'L'
        when C/count>0.5 then 'C'
        else 'Undetermined' END as side
from positions_tmp2;


drop table if exists players_tmp1;

Create temporary table players_tmp1 
	select distinct team, player_jersey from bdc2026_tracking_cleaned;


drop table if exists bdc2026_players;

Create table bdc2026_players select p.team, p.player_jersey, 
	COALESCE(count,0) as count,
	CASE when p.player_jersey='Go' then 'G' else COALESCE(position,'Undetermined') END as position, 
    COALESCE(side,'Undetermined') as side,
    row_number () OVER(order by team, player_jersey) as player_id
from players_tmp1 as p
left join positions_tmp3 as pos
	on p.player_jersey=pos.player_jersey and p.team=pos.team
where p.player_jersey<>'';




-- ============================================================================
-- 12. Attach the state of the game to every frame
-- ----------------------------------------------------------------------------
-- The tracking rows are matched to the windows built above, and the defensive role
-- is set: Primary is the defender nearest the puck or any defender within six feet
-- of it, and everyone else defending is Secondary.
-- ============================================================================
-- Add strength_state and score_state to the final cleaned tracking data table. 
-- Defense_role is defined as the defensive player closest to the puck or any defensive players within 6 feet of the puck. All other defenders are categorized as Secondary defenders. 
drop table if exists tracking_tmp7;

Create temporary table tracking_tmp7 select t.game_id, period, image_id, frame, time_elapsed, venue, t.team, 
	p.player_id, p.position, t.player_jersey, 
    CASE when t.player_jersey='Go' then 1 else 0 END as is_goalie,
    x_feet, y_feet, x_puck_feet, y_puck_feet, 
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
		else home_score_state END as score_state,
	row_number() OVER(order by image_id, p.player_id) as id
from tracking_tmp6 as t
left join strength_tmp5 as s
	on t.game_id=s.game_id and t.time_elapsed>=s.start_time and t.time_elapsed<=s.end_time
left join goals_tmp3 as g
	on t.game_id=g.game_id and t.time_elapsed>=g.start_time and t.time_elapsed<=g.end_time
left join bdc2026_players as p
	on t.team=p.team and t.player_jersey=p.player_jersey
order by image_id, player_id;




-- ============================================================================
-- 13. Remove duplicate player rows
-- ----------------------------------------------------------------------------
-- The tracking table must hold one row per player per frame. Where the join above
-- produced more than one, the row kept is the player nearest the puck.
-- ============================================================================
-- Create a copy of the table since you can't self join on a temporary table. 
drop table if exists tracking_tmp7_copy;

Create temporary table tracking_tmp7_copy select * from tracking_tmp7;


-- Delete all rows with duplicate player_ids in the same image_id. Keep the player closest to the puck if the position differs. 
-- There are 9,357 duplicate rows. 
alter table tracking_tmp7      add index ix_img_pid (image_id(40), player_id);
alter table tracking_tmp7_copy add index ix_img_pid (image_id(40), player_id);

delete a
from tracking_tmp7 a
join tracking_tmp7_copy b
  on a.image_id  = b.image_id
 and a.player_id = b.player_id
 and a.player_id is not null
 and (
       (a.distance_to_puck is null and b.distance_to_puck is not null)
    or (a.distance_to_puck > b.distance_to_puck)
    or ((a.distance_to_puck <=> b.distance_to_puck) and a.id > b.id)
     );



drop table if exists tracking_tmp8;

CREATE TABLE `tracking_tmp8` (
  `id`                   bigint unsigned  NOT NULL AUTO_INCREMENT,
  `game_id`              varchar(30)      NOT NULL,
  `period`               varchar(3)       NOT NULL,
  `image_id`             varchar(35)      NOT NULL,
  `frame`                int unsigned     NOT NULL,
  `time_elapsed`         smallint unsigned     NULL,
  `venue`                varchar(5)       NOT NULL,
  `team`                 varchar(20)      NOT NULL,
  `player_id`            int unsigned          NULL,
  `position`             varchar(12)           NULL,
  `player_jersey`        varchar(3)            NULL,
  `is_goalie`            tinyint unsigned NOT NULL DEFAULT 0,
  `x_feet`               double                NULL,
  `y_feet`               double                NULL,
  `x_puck_feet`          double                NULL,
  `y_puck_feet`          double                NULL,
  `distance_to_puck`     double                NULL,
  `distance_rank`        tinyint unsigned      NULL,
  `distance_rank_team`   tinyint unsigned      NULL,
  `player_in_possession` tinyint unsigned NOT NULL DEFAULT 0,
  `team_in_possession`   tinyint unsigned NOT NULL DEFAULT 0,
  `defense_role`         varchar(9)       NOT NULL DEFAULT '',
  `strength_state`       varchar(10)           NULL,
  `score_state`          tinyint               NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_frame_player` (`image_id`, `player_id`),
  KEY `ix_game_time` (`game_id`, `time_elapsed`),
  KEY `ix_player_game_time` (`player_id`, `game_id`, `time_elapsed`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- Insert the tracking data into the created table with keys. 
Insert into tracking_tmp8 
	(game_id, period, image_id, frame, time_elapsed, venue, team, player_id,
	position, player_jersey, is_goalie, x_feet, y_feet, x_puck_feet, y_puck_feet,
	distance_to_puck, distance_rank, distance_rank_team, player_in_possession,
	team_in_possession, defense_role, strength_state, score_state)
select game_id, period, image_id, frame, time_elapsed, venue, team, 
	player_id, position, player_jersey, is_goalie,
    x_feet, y_feet, x_puck_feet, y_puck_feet, 
    distance_to_puck, distance_rank, distance_rank_team, player_in_possession, team_in_possession,
    defense_role, strength_state, score_state
from tracking_tmp7
order by image_id, player_id;


analyze table bdc2026_tracking_cleaned;


drop table if exists bdc2026_shifts_raw;

create table bdc2026_shifts_raw (
  `Date`         varchar(20),
  `Home_Team`    varchar(40),
  `Away_Team`    varchar(40),
  `Team`         varchar(40),
  `Player_Id`    varchar(10),
  `shift_number` varchar(10),
  `period`       varchar(10),
  `start_clock`  varchar(10),
  `end_clock`    varchar(10),
  `shift_length` varchar(10)
);


-- Load shift data into the bdc2026_shifts table. 
-- The file names are hardcoded. 
load data infile
'2025-11-28.Team.J.@.Team.K.Shifts.csv'
into table moncton.bdc2026_shifts_raw
FIELDS
    TERMINATED BY ','
    OPTIONALLY ENCLOSED BY '"'
  LINES
    TERMINATED BY '\r\n'
  IGNORE 1 LINES;


drop table if exists shifts_tmp1;

Create temporary table shifts_tmp1 select date, CONCAT(date,' ',away_team, ' @ ', home_team) as game_id, 
	home_team, away_team, team, player_id as player_jersey, shift_number, period,
    CASE 
		when Period='1' then 1200-CAST(SUBSTRING_INDEX(start_clock,':',1)*60+SUBSTRING_INDEX(start_clock,':',-1) as signed)
        when Period='2' then 1200-CAST(SUBSTRING_INDEX(start_clock,':',1)*60+SUBSTRING_INDEX(start_clock,':',-1) as signed)+1200
        when Period='3' then 1200-CAST(SUBSTRING_INDEX(start_clock,':',1)*60+SUBSTRING_INDEX(start_clock,':',-1) as signed)+2400
        when Period='4' then 300-CAST(SUBSTRING_INDEX(start_clock,':',1)*60+SUBSTRING_INDEX(start_clock,':',-1) as signed)+3600 
        end as start_time,
	CASE 
		when Period='1' then 1200-CAST(SUBSTRING_INDEX(end_clock,':',1)*60+SUBSTRING_INDEX(end_clock,':',-1) as signed)
        when Period='2' then 1200-CAST(SUBSTRING_INDEX(end_clock,':',1)*60+SUBSTRING_INDEX(end_clock,':',-1) as signed)+1200
        when Period='3' then 1200-CAST(SUBSTRING_INDEX(end_clock,':',1)*60+SUBSTRING_INDEX(end_clock,':',-1) as signed)+2400
        when Period='4' then 300-CAST(SUBSTRING_INDEX(end_clock,':',1)*60+SUBSTRING_INDEX(end_clock,':',-1) as signed)+3600 
        end as end_time
from bdc2026_shifts_raw;


drop table if exists shifts_tmp2;

Create temporary table shifts_tmp2 select date, game_id, home_team, away_team, s.team, 
	p.player_id, s.player_jersey, shift_number, period, 
	start_time, end_time, end_time - start_time as shift_length
from shifts_tmp1 as s
left join bdc2026_players as p
	on s.team=p.team and s.player_jersey=p.player_jersey;


-- Change player_ids in the event data to actual player ids instead of jersey numbers. 
drop table if exists events_tmp2;

Create table events_tmp2 
select date, home_team, away_team, period, time_elapsed, 
	venue, e.team, strength_state, score_state, p1.player_id, event, 
    CASE 
		when GoalieTeamOnRightSideOfRink1stPeriod='Home' and period=2 then -x1
        when GoalieTeamOnRightSideOfRink1stPeriod='Away' and (period=1 or period=3) then -x1
        when period=4 and venue='Home' then -x1
        else x1 END as x1, 
	CASE 
		when GoalieTeamOnRightSideOfRink1stPeriod='Home' and period=2 then -y1
        when GoalieTeamOnRightSideOfRink1stPeriod='Away' and (period=1 or period=3) then -y1
        when period=4 and venue='Home' then -y1
        else y1 END as y1, 
    detail_1, detail_2, detail_3, detail_4, 
    p2.player_id as player_id_2, 
    CASE 
		when GoalieTeamOnRightSideOfRink1stPeriod='Home' and period=2 then -x2
        when GoalieTeamOnRightSideOfRink1stPeriod='Away' and (period=1 or period=3) then -x2
        when period=4 and venue='Home' then -x2
        else x2 END as x2, 
	CASE 
		when GoalieTeamOnRightSideOfRink1stPeriod='Home' and period=2 then -y2
        when GoalieTeamOnRightSideOfRink1stPeriod='Away' and (period=1 or period=3) then -y2
        when period=4 and venue='Home' then -y2
        else y2 END as y2, 
    is_corsi, is_fenwick, is_shot, is_goal, event_index
from events_tmp1 as e
left join bdc2026_players as p1
	on e.team=p1.team and e.player_id=p1.player_jersey
left join bdc2026_players as p2
	on e.team=p2.team and e.player_id_2=p2.player_jersey
left join bdc2026_camera as c
	on CONCAT(e.date,' ',e.away_team, ' @ ', e.home_team)=c.Game;


drop table if exists events_tmp3;

Create temporary table events_tmp3 
	select date, home_team, away_team, period, time_elapsed, 
	venue, team, strength_state, score_state, player_id,
    CASE 
		when event='Shot' and detail_2='Blocked' then 'Blocked Shot'
        when event='Shot' and detail_2='Missed' then 'Missed Shot'
        when event='Shot' and detail_2='On Net' then 'Shot on Net'
		else event END as event,
	x1, y1, detail_1, detail_2, detail_3, detail_4, player_id_2, 
    x2, y2, is_corsi, is_fenwick, is_shot, is_goal, event_index
from events_tmp2
UNION ALL
select date, home_team, away_team, period, time_elapsed, 
	venue, team, strength_state, score_state, player_id_2 as player_id,
    'Reception' as event, x2 as x1, y2 as y1, 
    detail_1, detail_2, detail_3, detail_4, NULL as player_id_2, 
    NULL as x2, NULL as y2, is_corsi, is_fenwick, is_shot, is_goal, event_index
from events_tmp2
where event='Play'
UNION ALL
select date, home_team, away_team, period, time_elapsed, 
	venue, team, strength_state, score_state, player_id_2 as player_id,
    'Reception Failed' as event, x2 as x1, y2 as y1, 
    detail_1, detail_2, detail_3, detail_4, NULL as player_id_2, 
    NULL as x2, NULL as y2, is_corsi, is_fenwick, is_shot, is_goal, event_index
from events_tmp2
where event='Incomplete Play';

drop table if exists events_tmp2;

drop table if exists events_tmp4;

Create temporary table events_tmp4 select date, home_team, away_team, period, time_elapsed, 
	venue, team, strength_state, score_state, player_id, event, x1, y1, 
    detail_1, detail_2, detail_3, detail_4, player_id_2, 
    x2, y2, is_corsi, is_fenwick, is_shot, is_goal,
    row_number() OVER(order by event_index, event) as event_index
from events_tmp3;


drop table if exists events_tmp5;

Create temporary table events_tmp5 select e.date, 
	CONCAT(e.date,' ',e.away_team, ' @ ', e.home_team) as game_id, 
    e.home_team, e.away_team, e.period, time_elapsed, 
	venue, e.team, strength_state, score_state, e.player_id, event, x1, y1, 
    detail_1, detail_2, detail_3, detail_4, player_id_2, x2, y2, 
    is_corsi, is_fenwick, is_shot, is_goal, event_index, 
    CASE when e.team=s.team then 'For' else 'Against' END as for_against, 
    s.player_id as players_on,
    row_number() OVER(partition by s.team, event_index order by s.player_id) as ranking
from events_tmp4 as e
left join shifts_tmp2 as s
	on e.date=s.date and e.home_team=s.home_team and e.away_team=s.away_team and 
		((e.event='Faceoff Win' and e.time_elapsed>=s.start_time and e.time_elapsed<s.end_time) or
        (e.event<>'Faceoff Win' and e.time_elapsed>s.start_time and e.time_elapsed<=s.end_time));



drop table if exists bdc2026_events_cleaned;

CREATE TABLE `bdc2026_events_cleaned` (
  `event_index`      int unsigned      NOT NULL,
  `game_id`          varchar(30)       NOT NULL,
  `game_date`        date              NOT NULL,
  `home_team`        varchar(20)       NOT NULL,
  `away_team`        varchar(20)       NOT NULL,
  `period`           varchar(3)        NOT NULL,
  `time_elapsed`     smallint unsigned NOT NULL,
  `venue`            varchar(5)        NOT NULL,
  `team`             varchar(20)       NOT NULL,
  `strength_state`   varchar(10)       NOT NULL,
  `score_state`      tinyint           NOT NULL,
  `player_id`        int unsigned           NULL,
  `event`            varchar(20)       NOT NULL,
  `x1`               double            NOT NULL,
  `y1`               double            NOT NULL,
  `detail_1`         varchar(30)            NULL,
  `detail_2`         varchar(30)            NULL,
  `detail_3`         varchar(30)            NULL,
  `detail_4`         varchar(30)            NULL,
  `player_id_2`      int unsigned           NULL,
  `x2`               double                 NULL,
  `y2`               double                 NULL,
  `is_corsi`         tinyint unsigned  NOT NULL DEFAULT 0,
  `is_fenwick`       tinyint unsigned  NOT NULL DEFAULT 0,
  `is_shot`          tinyint unsigned  NOT NULL DEFAULT 0,
  `is_goal`          tinyint unsigned  NOT NULL DEFAULT 0,
  `for_1`            int unsigned           NULL,
  `for_2`            int unsigned           NULL,
  `for_3`            int unsigned           NULL,
  `for_4`            int unsigned           NULL,
  `for_5`            int unsigned           NULL,
  `for_6`            int unsigned           NULL,
  `against_1`        int unsigned           NULL,
  `against_2`        int unsigned           NULL,
  `against_3`        int unsigned           NULL,
  `against_4`        int unsigned           NULL,
  `against_5`        int unsigned           NULL,
  `against_6`        int unsigned           NULL,
  PRIMARY KEY (`event_index`),
  KEY `ix_game_time` (`game_id`, `time_elapsed`),
  KEY `ix_player` (`player_id`, `game_id`),
  KEY `ix_event` (`event`, `game_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;



Insert into bdc2026_events_cleaned 
select event_index, game_id, date as game_date, 
	home_team, away_team, period, time_elapsed, 
	venue, team, strength_state, score_state, player_id, event, x1, y1, 
    detail_1, detail_2, detail_3, detail_4, 
    CAST(nullif(player_id_2,'') as unsigned) as player_id_2, 
    cast(nullif(x2,'') as double) as x2, 
    cast(nullif(y2,'') as double) as y2, 
    is_corsi, is_fenwick, is_shot, is_goal,
    MAX(CASE when ranking=1 and for_against='For' then players_on else NULL END) as for_1, 
    MAX(CASE when ranking=2 and for_against='For' then players_on else NULL END) as for_2,
    MAX(CASE when ranking=3 and for_against='For' then players_on else NULL END) as for_3,
    MAX(CASE when ranking=4 and for_against='For' then players_on else NULL END) as for_4,
    MAX(CASE when ranking=5 and for_against='For' then players_on else NULL END) as for_5,
    MAX(CASE when ranking=6 and for_against='For' then players_on else NULL END) as for_6,
    MAX(CASE when ranking=1 and for_against='Against' then players_on else NULL END) as against_1,
    MAX(CASE when ranking=2 and for_against='Against' then players_on else NULL END) as against_2,
    MAX(CASE when ranking=3 and for_against='Against' then players_on else NULL END) as against_3,
    MAX(CASE when ranking=4 and for_against='Against' then players_on else NULL END) as against_4,
    MAX(CASE when ranking=5 and for_against='Against' then players_on else NULL END) as against_5,
    MAX(CASE when ranking=6 and for_against='Against' then players_on else NULL END) as against_6
from events_tmp5
group by date, game_id, home_team, away_team, period, time_elapsed, 
	venue, team, strength_state, score_state, player_id, event, x1, y1, 
    detail_1, detail_2, detail_3, detail_4, player_id_2, x2, y2, 
    is_corsi, is_fenwick, is_shot, is_goal, event_index;


drop temporary table if exists event_frames;

create temporary table event_frames
select
  x.*,
  coalesce(x.player_image_id, x.fallback_image_id) as image_id,
  case when x.player_image_id   is not null then 'player'
       when x.fallback_image_id is not null then 'nearest_player'
       else 'none' end as frame_match
from (
    select
      s.event_index, s.game_id, s.time_elapsed, s.team, s.event,
      s.x1, s.y1, s.player_id,
      (
        select t.image_id
        from tracking_tmp8 t
        where t.game_id      = s.game_id
          and t.time_elapsed = s.time_elapsed
          and t.player_id    = s.player_id
        order by abs(t.x_feet - s.x1) + abs(t.y_feet - s.y1), t.image_id
        limit 1
      ) as player_image_id,
      (
        select t.image_id
        from tracking_tmp8 t
        where t.game_id      = s.game_id
          and t.time_elapsed = s.time_elapsed
          and t.team         = s.team          -- the event's own side, not an opponent
          and t.x_feet is not null
        order by abs(t.x_feet - s.x1) + abs(t.y_feet - s.y1), t.image_id
        limit 1
      ) as fallback_image_id
    from bdc2026_events_cleaned s
) x;


drop temporary table if exists event_frames_nudged;

create temporary table event_frames_nudged
select
  event_index, game_id, time_elapsed, team, event, x1, y1, player_id,
  frame_match, rn,
  concat(left(image_id, 27),
         lpad(cast(right(image_id, 6) as unsigned) + rn - 1, 6, '0')) as image_id
from (
    select f.*, row_number() over (partition by f.image_id order by f.event_index) as rn
    from event_frames f
) x;

drop temporary table if exists event_frames_final;

create temporary table event_frames_final
select
  event_index, game_id, time_elapsed, team, event, x1, y1, player_id, frame_match,
  concat(left(image_id, 27),
         lpad(cast(right(image_id, 6) as unsigned) + rn - 1, 6, '0')) as image_id
from (
    select
      f.event_index, f.game_id, f.time_elapsed, f.team, f.event,
      f.x1, f.y1, f.player_id, f.frame_match, f.image_id,
      row_number() over (partition by f.image_id order by f.event_index) as rn
    from event_frames_nudged f
) x;

drop temporary table if exists event_frames_final2;

create temporary table event_frames_final2
select
  event_index, game_id, time_elapsed, team, event, x1, y1, player_id, frame_match,
  concat(left(image_id, 27),
         lpad(cast(right(image_id, 6) as unsigned) + rn - 1, 6, '0')) as image_id
from (
    select
      f.event_index, f.game_id, f.time_elapsed, f.team, f.event,
      f.x1, f.y1, f.player_id, f.frame_match, f.image_id,
      row_number() over (partition by f.image_id order by f.event_index) as rn
    from event_frames_final f
) x;

drop temporary table if exists event_frames_final3;

create temporary table event_frames_final3
select
  event_index, game_id, time_elapsed, team, event, x1, y1, player_id, frame_match,
  concat(left(image_id, 27),
         lpad(cast(right(image_id, 6) as unsigned) + rn - 1, 6, '0')) as image_id
from (
    select
      f.event_index, f.game_id, f.time_elapsed, f.team, f.event,
      f.x1, f.y1, f.player_id, f.frame_match, f.image_id,
      row_number() over (partition by f.image_id order by f.event_index) as rn
    from event_frames_final2 f
) x;

alter table event_frames_final3
  add unique index ix_image (image_id),
  add unique index ix_event (event_index);

-- how the frames were chosen
select frame_match, count(*) as events
from event_frames_final3 group by frame_match;

-- nudges that point at a frame the tracking does not have
select count(*) as frame_does_not_exist
from event_frames_final3 f
left join (select distinct game_id, image_id from tracking_tmp8) t
       on t.game_id = f.game_id and t.image_id = f.image_id
where f.image_id is not null and t.image_id is null;


drop table if exists bdc2026_tracking_cleaned;

CREATE TABLE `bdc2026_tracking_cleaned` (
  `id`                   bigint unsigned  NOT NULL AUTO_INCREMENT,
  `game_id`              varchar(30)      NOT NULL,
  `period`               varchar(3)       NOT NULL,
  `image_id`             varchar(35)      NOT NULL,
  `frame`                int unsigned     NOT NULL,
  `time_elapsed`         smallint unsigned     NULL,
  `venue`                varchar(5)       NOT NULL,
  `team`                 varchar(20)      NOT NULL,
  `player_id`            int unsigned          NULL,
  `position`             varchar(12)           NULL,
  `player_jersey`        varchar(3)            NULL,
  `is_goalie`            tinyint unsigned NOT NULL DEFAULT 0,
  `x_feet`               double                NULL,
  `y_feet`               double                NULL,
  `x_puck_feet`          double                NULL,
  `y_puck_feet`          double                NULL,
  `distance_to_puck`     double                NULL,
  `distance_rank`        tinyint unsigned      NULL,
  `distance_rank_team`   tinyint unsigned      NULL,
  `player_in_possession` tinyint unsigned NOT NULL DEFAULT 0,
  `team_in_possession`   tinyint unsigned NOT NULL DEFAULT 0,
  `defense_role`         varchar(9)       NOT NULL DEFAULT '',
  `strength_state`       varchar(10)           NULL,
  `score_state`          tinyint               NULL,
  `event_index`			 int unsigned          NULL,
  `event`            	 varchar(20)           NULL,
  `zone`				 varchar(3)			   NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_frame_player` (`image_id`, `player_id`),
  KEY `ix_game_time` (`game_id`, `time_elapsed`),
  KEY `ix_player_game_time` (`player_id`, `game_id`, `time_elapsed`),
  KEY `ix_event_index` (`event_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


Insert into bdc2026_tracking_cleaned
	(game_id, period, image_id, frame, time_elapsed, venue, team, player_id,
	position, player_jersey, is_goalie, x_feet, y_feet, x_puck_feet, y_puck_feet,
	distance_to_puck, distance_rank, distance_rank_team, player_in_possession,
	team_in_possession, defense_role, strength_state, score_state, event_index, event, zone)
select t.game_id, period, t.image_id, frame, t.time_elapsed, venue, t.team, 
	t.player_id, position, player_jersey, is_goalie,
    x_feet, y_feet, x_puck_feet, y_puck_feet, 
    distance_to_puck, distance_rank, distance_rank_team, player_in_possession, team_in_possession,
    defense_role, strength_state, score_state, e.event_index, e.event,
    CASE 
		when x_puck_feet>=25 then 'dz'
        when x_puck_feet<=-25 then 'oz'
        when x_puck_feet>-25 or x_puck_feet<25 then 'nz'
        else NULL END as zone
from tracking_tmp8 as t
left join event_frames_final3 as e
	on t.image_id=e.image_id
order by id;
    


Create temporary table metrics_tmp select player_id,
	COUNT(*)/30 as tracked_time, 
    SUM(CASE when venue='Home' and zone='dz' then 1/30 when venue='Away' and zone='oz' then 1/30 else 0 END) as time_dz,
    SUM(CASE when venue='Home' and zone='oz' then 1/30 when venue='Away' and zone='dz' then 1/30 else 0 END) as time_oz,
    SUM(CASE when venue='Home' and zone='dz' and team_in_possession=0 then 1/30 
		when venue='Away' and zone='oz' and team_in_possession=0 then 1/30 else 0 END) as time_dz_def,
	SUM(CASE when venue='Home' and zone='dz' and defense_role='Primary' then 1/30 
		when venue='Away' and zone='oz' and defense_role='Primary' then 1/30 else 0 END) as time_dz_primary,
	AVG(CASE when venue='Home' then -x_feet else x_feet END) as x_avg,
    AVG(CASE when venue='Home' and team_in_possession=0 and zone='dz' then -x_feet 
		when venue='Away' and team_in_possession=0 and zone='oz' then x_feet else NULL END) as x_avg_dz_def,
    AVG(CASE when venue='Home' and team_in_possession=1 and zone='oz' then -x_feet 
		when venue='Away' and team_in_possession=1 and zone='dz' then x_feet else NULL END) as x_avg_oz_off,
	AVG(CASE when venue='Home' and zone='dz' and defense_role='Primary' then distance_to_puck
		when venue='Away' and zone='oz' and defense_role='Primary' then distance_to_puck else NULL END) as dist_puck_primary,
	AVG(CASE when venue='Home' and zone='dz' and defense_role='Secondary' then distance_to_puck
		when venue='Away' and zone='oz' and defense_role='Secondary' then distance_to_puck else NULL END) as dist_puck_secondary,
	AVG(CASE when venue='Home' and team_in_possession=0 and zone='dz'
		then SQRT(POW(x_feet - 89, 2) + POW(y_feet, 2))
		when venue='Away' and team_in_possession=0 and zone='oz'
		then SQRT(POW(x_feet + 89, 2) + POW(y_feet, 2)) else NULL END) as dist_net_dz_def,
	AVG(CASE when venue='Home' and zone='dz' and defense_role='Primary'
		then SQRT(POW(x_feet - 89, 2) + POW(y_feet, 2))
		when venue='Away' and zone='oz' and defense_role='Primary'
		then SQRT(POW(x_feet + 89, 2) + POW(y_feet, 2)) else NULL END) as dist_net_primary,
	AVG(CASE when venue='Home' and zone='dz' and defense_role='Primary'
		then SQRT(POW(x_puck_feet - 89, 2) + POW(y_puck_feet, 2))
		when venue='Away' and zone='oz' and defense_role='Primary'
		then SQRT(POW(x_puck_feet + 89, 2) + POW(y_puck_feet, 2)) else NULL END) as puck_dist_net_primary,
    SUM(CASE when venue='Home' and zone='dz' and event='Blocked Shot' and team_in_possession=0 then 1
		when venue='Away' and zone='oz' and event='Blocked Shot' and team_in_possession=0 then 1 else 0 END) as blocked_against,
	SUM(CASE when venue='Home' and zone='dz' and event='Blocked Shot' and team_in_possession=0 then 1
		when venue='Away' and zone='oz' and event='Blocked Shot' and defense_role='Primary' then 1 else 0 END) as blocked_primary,
	SUM(CASE when venue='Home' and zone='dz' and event='Missed Shot' and team_in_possession=0 then 1
		when venue='Away' and zone='oz' and event='Missed Shot' and team_in_possession=0 then 1 else 0 END) as missed_against,
	SUM(CASE when venue='Home' and zone='dz' and event='Missed Shot' and team_in_possession=0 then 1
		when venue='Away' and zone='oz' and event='Missed Shot' and defense_role='Primary' then 1 else 0 END) as missed_primary,
	SUM(CASE when venue='Home' and zone='dz' and event='Shot on Net' and team_in_possession=0 then 1
		when venue='Away' and zone='oz' and event='Shot on Net' and team_in_possession=0 then 1 else 0 END) as on_net_against,
	SUM(CASE when venue='Home' and zone='dz' and event='Shot on Net' and team_in_possession=0 then 1
		when venue='Away' and zone='oz' and event='Shot on Net' and defense_role='Primary' then 1 else 0 END) as on_net_primary,
	SUM(CASE when venue='Home' and zone='dz' and event='Goal' and team_in_possession=0 then 1
		when venue='Away' and zone='oz' and event='Goal' and team_in_possession=0 then 1 else 0 END) as goal_against,
	SUM(CASE when venue='Home' and zone='dz' and event='Goal' and team_in_possession=0 then 1
		when venue='Away' and zone='oz' and event='Goal' and defense_role='Primary' then 1 else 0 END) as goal_primary
from bdc2026_tracking_cleaned
where is_goalie=0 and strength_state='5v5'
group by player_id;


alter table bdc2026_players
    add column tracked_time          decimal(10,2) null,
    add column time_dz               decimal(10,2) null,
    add column time_oz               decimal(10,2) null,
    add column time_dz_def           decimal(10,2) null,
    add column time_dz_primary       decimal(10,2) null,
    add column x_avg                 decimal(8,2)  null,
    add column x_avg_dz_def          decimal(8,2)  null,
    add column x_avg_oz_off          decimal(8,2)  null,
    add column dist_puck_primary     decimal(8,2)  null,
    add column dist_puck_secondary   decimal(8,2)  null,
    add column puck_dist_net_primary decimal(8,2)  null,
    add column dist_net_dz_def       decimal(8,2)  null,
    add column dist_net_primary      decimal(8,2)  null,
    add column blocked_against       int unsigned  null,
    add column blocked_primary       int unsigned  null,
    add column missed_against        int unsigned  null,
    add column missed_primary        int unsigned  null,
    add column on_net_against        int unsigned  null,
    add column on_net_primary        int unsigned  null,
    add column goal_against          int unsigned  null,
    add column goal_primary          int unsigned  null;



update bdc2026_players p
join metrics_tmp m on m.player_id = p.player_id
set p.tracked_time          = m.tracked_time,
    p.time_dz               = m.time_dz,
    p.time_oz               = m.time_oz,
    p.time_dz_def           = m.time_dz_def,
    p.time_dz_primary       = m.time_dz_primary,
    p.x_avg                 = m.x_avg,
    p.x_avg_dz_def          = m.x_avg_dz_def,
    p.x_avg_oz_off          = m.x_avg_oz_off,
    p.dist_puck_primary     = m.dist_puck_primary,
    p.dist_puck_secondary   = m.dist_puck_secondary,
    p.puck_dist_net_primary = m.puck_dist_net_primary,
    p.dist_net_dz_def       = m.dist_net_dz_def,
    p.dist_net_primary      = m.dist_net_primary,
    p.blocked_against       = m.blocked_against,
    p.blocked_primary       = m.blocked_primary,
    p.missed_against        = m.missed_against,
    p.missed_primary        = m.missed_primary,
    p.on_net_against        = m.on_net_against,
    p.on_net_primary        = m.on_net_primary,
    p.goal_against          = m.goal_against,
    p.goal_primary          = m.goal_primary;
