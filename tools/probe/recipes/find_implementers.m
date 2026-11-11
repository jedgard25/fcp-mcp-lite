#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  int n=objc_getClassList(NULL,0); Class *all=(Class*)malloc(sizeof(Class)*n); n=objc_getClassList(all,n);
  for(NSString *s in a[@"sels"]){ NSMutableArray *h=[NSMutableArray new]; SEL x=NSSelectorFromString(s);
    for(int i=0;i<n;i++){ unsigned mc=0; Method *ms=class_copyMethodList(all[i],&mc); for(unsigned j=0;j<mc;j++) if(method_getName(ms[j])==x) [h addObject:[NSString stringWithFormat:@"-%s %s",class_getName(all[i]),method_getTypeEncoding(ms[j])]]; free(ms);
      ms=class_copyMethodList(object_getClass(all[i]),&mc); for(unsigned j=0;j<mc;j++) if(method_getName(ms[j])==x) [h addObject:[NSString stringWithFormat:@"+%s %s",class_getName(all[i]),method_getTypeEncoding(ms[j])]]; free(ms);}
    r[s]=h; }
  free(all);
  r[@"FFChannelChangeController"]=[findSels(objc_getClass("FFChannelChangeController"),@"",NO) arrayByAddingObjectsFromArray:findSels(objc_getClass("FFChannelChangeController"),@"",YES)];
  return r;
}
