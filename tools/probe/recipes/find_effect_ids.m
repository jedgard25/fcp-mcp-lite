#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  Class reg=objc_getClass("FFEffectRegistry"); r[@"reg+"]=findSels(reg,@"",YES);
  id shared=CALL0((id)reg,@"registry")?:CALL0((id)reg,@"sharedRegistry")?:CALL0((id)reg,@"sharedInstance");
  r[@"shared"]=D(shared);
  NSMutableArray *hits=[NSMutableArray new];
  id ids=CALL0(shared,@"allEffectIDs")?:CALL0(shared,@"effectIDs")?:CALL1(CLS("FFEffect"),@"userVisibleEffectIDs",nil);
  for(id e in ids){ NSString *nm=D(CALL1(CLS("FFEffect"),@"displayNameForEffectID:",e)); if([nm rangeOfString:@"atur" options:1].location!=NSNotFound||[nm rangeOfString:@"Color Adjust" options:1].location!=NSNotFound) [hits addObject:[NSString stringWithFormat:@"%@ = %@",e,nm]]; }
  r[@"count"]=@([ids count]); r[@"hits"]=hits;
  r[@"FFEffect+"]=[findSels(objc_getClass("FFEffect"),@"ffectID",YES) subarrayWithRange:NSMakeRange(0,MIN((NSUInteger)40,[findSels(objc_getClass("FFEffect"),@"ffectID",YES) count]))];
  return r;
}
