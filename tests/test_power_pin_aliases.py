from agents.ratsnestpro.pin_evidence import pin_differences


def test_power_voltage_abbreviations_are_direction_scoped():
    rows=[{'number':'1','name':'VI','type':'power_in'}, {'number':'2','name':'VO','type':'power_out'}]
    table={'pins':[{'number':'1','functions':['VIN']}, {'number':'2','functions':['VOUT']}]}
    assert not pin_differences(rows,table)
    rows[0]['type']='input'
    assert pin_differences(rows,table)[0]['number']=='1'


def test_power_direction_and_rail_identity_never_disappear():
    for name,observed in [('VI','VOUT'),('VDD','VIN'),('NC','VIN')]:
        assert pin_differences([{'number':'1','name':name,'type':'power_in'}],
                               {'pins':[{'number':'1','functions':[observed]}]})
