from database import db
from db_models import Game, Season, Team, Play
from play_by_play import rebuild_advanced_team_stats


def seed():
    db.session.add(Season(year=2099))
    a=Team(external_id='phase10-1',abbreviation='AAA',name='Alpha')
    b=Team(external_id='phase10-2',abbreviation='BBB',name='Beta')
    db.session.add_all([a,b]); db.session.flush()
    g=Game(external_id='g1',season=2099,season_type='REG',week=1,home_team_id=a.id,away_team_id=b.id)
    db.session.add(g); db.session.flush()
    db.session.add_all([
      Play(external_id='p1',game_id=g.id,sequence=1,offense_team_id=a.id,defense_team_id=b.id,down=1,yards_gained=8,epa=.5,success=True),
      Play(external_id='p2',game_id=g.id,sequence=2,offense_team_id=a.id,defense_team_id=b.id,down=3,yards_gained=2,epa=-.2,success=False),
    ])
    db.session.commit()


def test_readiness(client):
    assert client.get('/ready').status_code == 200


def test_advanced_aggregation(client):
    with client.application.app_context():
        seed(); result=rebuild_advanced_team_stats(2099)
        assert result['plays']==2
        assert result['rows']==2


def test_admin_overview(client):
    assert client.get('/api/admin/overview').status_code == 200
