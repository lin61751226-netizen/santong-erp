from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Worksite, WorksiteForkliftRate
from app.services.bootstrap import _ensure_guotai_ciwen_rates, _ensure_worksites


def test_guotai_ciwen_site_and_rate_card_are_idempotent():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        sites = _ensure_worksites(session)
        _ensure_guotai_ciwen_rates(session, sites)
        site = sites['國泰慈文']
        assert site.code == '國泰慈文' and site.is_active
        assert site.address == '桃園市桃園區中路二段'
        rates = session.exec(select(WorksiteForkliftRate).where(WorksiteForkliftRate.worksite_id == site.id)).all()
        assert {(item.vehicle_type, item.weekday_hourly, item.weekday_daily,
                 item.holiday_hourly, item.holiday_daily, item.overtime_hourly) for item in rates} == {
            ('2.5噸（2節桿）', 1200, 8000, 1500, 10000, 1500),
            ('2.5噸（高4.5M）', 1800, 12000, 2000, 14000, 2000),
        }
        site.is_active = False
        session.add(site)
        session.commit()
        _ensure_guotai_ciwen_rates(session, _ensure_worksites(session))
        assert not session.exec(select(Worksite).where(Worksite.code == '國泰慈文')).one().is_active
        assert len(session.exec(select(WorksiteForkliftRate).where(WorksiteForkliftRate.worksite_id == site.id)).all()) == 2
    engine.dispose()
