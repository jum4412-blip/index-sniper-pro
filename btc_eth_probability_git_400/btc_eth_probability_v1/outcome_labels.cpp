#include <algorithm>
#include <cmath>
#include <cstdint>
// Independent hypothetical event labels. This is NOT a portfolio P&L engine.
namespace {
constexpr int64_t MIN=60000;
double roundv(double v,double step,bool up){return (up?std::ceil(v/step-1e-10):std::floor(v/step+1e-10))*step;}
int64_t fundbound(const double*f,int64_t n,int64_t t){int64_t l=0,r=n;while(l<r){auto m=(l+r)/2;if(f[m*2]<=t)l=m+1;else r=m;}return l;}
}
extern "C" int label_events(const double*raw,int64_t n,const double*fund,int64_t fn,const double*ev,int64_t en,int symbol,double cost,int path,double*out){
 if(n<2||cost<=0)return 1;
 const double step=symbol==0?.0001:.01,tick=symbol==0?.1:.01,half=.0001*cost,fee=.0006*cost;
 const int64_t origin=(int64_t)raw[0];
 for(int64_t k=0;k<en;k++){
  const double*e=ev+5*k;double*r=out+22*k;std::fill(r,r+22,0.);
  int64_t entry=(int64_t)e[0],i=(entry-origin)/MIN;int side=(int)e[1];double signal=e[2],stop=roundv(e[3],tick,side==-1);int64_t expiry=entry+(int64_t)e[4]*MIN;
  r[0]=entry;r[1]=entry;r[18]=0;r[19]=0;r[20]=0;r[21]=0;
  if(entry!=origin+i*MIN||i<0||i>=n||side*side!=1||expiry>=origin+n*MIN){r[18]=11;continue;}
  const double*b=raw+7*i;double mid=b[1];r[2]=mid;r[5]=stop;
  if(!std::isfinite(stop)||stop<=0||!std::isfinite(signal)||signal<=0||side*(mid-stop)<=tick){r[18]=12;continue;}
  if(std::abs(mid-signal)/signal>.001){r[18]=13;continue;}
  int64_t fp=fundbound(fund,fn,entry);double last_rate=fp?fund[2*(fp-1)+1]:0;
  if(side*last_rate>.0005){r[18]=14;continue;}
  double base=mid*(1+side*half),fill=roundv(base*(1+side*.0005*cost),tick,side==1),qty=roundv(2500./std::max(base,fill),step,false);
  double dist=side*(fill-stop),pct=dist/base,exstop=stop*(1-side*half)*(1-side*.001*cost);
  double risk=qty*(side*(fill-exstop)+fee*(fill+exstop));
  r[3]=fill;r[4]=qty;r[7]=risk;r[20]=pct;
  if(qty<=0||dist<=0||pct<.002||pct>.008||!std::isfinite(risk)){r[18]=12;continue;}
  if(risk>25.+1e-8||risk<=0){r[18]=15;continue;}
  double target=roundv(fill+side*2*dist,tick,side==1);r[6]=target;
  double funding=0,exitref=0;int reason=0;int64_t closed=entry;
  for(int64_t j=i;j<n;j++){
   b=raw+7*j;int64_t t=(int64_t)b[0];mid=b[1];
   // Funding at the entry instant was settled before this hypothetical entry.
   if(j>i){while(fp<fn&&(int64_t)fund[2*fp]<=t){funding-=side*qty*mid*fund[2*fp+1];fp++;}}
   if(t>=expiry){exitref=mid;closed=t;reason=3;break;}
   if(j>i&&side*(mid-stop)<=0){exitref=mid;closed=t;reason=1;break;}
   if(j>i&&side*(mid-target)>=0){exitref=target;closed=t;reason=2;break;}
   double p1,p2;if(path==2){p1=side==1?b[3]:b[2];p2=side==1?b[2]:b[3];}else{p1=path==0?b[2]:b[3];p2=path==0?b[3]:b[2];}
   const double pts[4]={b[1],p1,p2,b[4]};const int64_t offsets[4]={0,20000,40000,59999};
   for(int seg=0;seg<3;seg++){
    int64_t left=t+offsets[seg],right=t+offsets[seg+1],hit=right+1;double lev=0;int why=0;
    double delta=pts[seg+1]-pts[seg];
    for(int z=0;z<2;z++){
     double level=z==0?stop:target;
     bool crosses=z==0?(side*(pts[seg]-level)>0&&side*(pts[seg+1]-level)<=0):(side*(pts[seg]-level)<0&&side*(pts[seg+1]-level)>=0);
     if(crosses&&delta!=0){double fraction=(level-pts[seg])/delta;int64_t at=left+(int64_t)std::ceil(fraction*double(right-left)-1e-9);if(at<hit){hit=at;lev=level;why=z+1;}}
    }
    int64_t until=why?hit:right;
    while(fp<fn&&(int64_t)fund[2*fp]<=until){int64_t ft=(int64_t)fund[2*fp];double mark;if(ft<=left)mark=pts[seg];else mark=pts[seg]+delta*double(ft-left)/double(right-left);funding-=side*qty*mark*fund[2*fp+1];fp++;}
    if(why){exitref=lev;closed=hit;reason=why;break;}
   }
   if(reason)break;
  }
  if(!reason){r[18]=11;continue;}
  double exitfill=exitref*(1-side*half)*(1-side*.001*cost),gross=side*(exitfill-fill)*qty,fees=fee*qty*(fill+exitfill),net=gross-fees+funding;
  r[1]=closed;r[8]=exitref;r[9]=exitfill;r[10]=gross;r[11]=fees;r[12]=funding;r[13]=net;r[14]=net/risk;r[15]=net>0?1:0;r[16]=double(closed-entry)/MIN;r[17]=gross/risk;r[18]=reason;r[19]=1;r[21]=qty*fill;
 }
 return 0;
}
